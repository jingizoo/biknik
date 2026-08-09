"""Setup CREATES must be judged by the axes they CONSUME (#409, create family).

THE DEFECT, MEASURED. A create is not a mutation on a target, so none of the
#409 machinery that guards delete/edit/reassign applied to it. Two shapes were
live:

  * the four PARENT-GATED creates (venue / rink / ice_slot / official) reached
    their write through ``Handler._reject_parent_outside_scope``, whose decision
    is ``ApiService.setup_parent_writable`` -> ``writable_setup_parent_ids`` ->
    ``get_setup_overview_v2`` -> ``resolve_with_league`` -> ``_fallback()``. So
    the gate that was supposed to stop a cross-owner write was itself
    authorized by a Program the fallback had just picked. Measured on this
    branch before the fix, with a spy on ``ContextService._fallback`` and a
    signed-in League Admin whose saved ``ActiveContext`` row was asserted
    ABSENT::

        saved ActiveContext row: None
        setup_parent_writable("club", A-Club) -> True   | _fallback calls: 1
        create_official -> official_1  home_club: club_1
        officials before/after: 0 -> 1
        official edges: ({('league_1', None, 'league_2')}, True)   # Program A

    i.e. the create was authorized by a Program the operator never chose,
    returned 200, and wrote the row plus its audit entry;

  * every OTHER create (season, league, division, game, player, the season
    team-registration, v1 team, the roll-forwards, the import commit, the demo
    add-ice-slot) reached the write with no context read at all — strictly
    worse.

THE OWNER'S RULING, three axis classes, encoded in
``ApiService._CREATE_CONSUMED_AXES``:

  * ZERO-AXIS ROOT — organization / program / club. A root with no parent;
    creating it is the act that would establish a context. NO context is read
    AT ALL, which ``ZeroAxisRootCreateTest`` proves with a spy on
    ``ContextService._fallback`` asserting zero calls. The spy is deliberately
    on ``_fallback`` and not on ``resolve``/``resolve_with_league``:
    ``_resolve_locked`` honours a valid saved row without ever reaching
    ``_fallback``, so a zero-call assertion on the resolver would pass for the
    wrong reason.
  * PROGRAM-AXIS — venue / rink / ice_slot / official / team / player, and
    ``season``. The saved Program is compared against the PARENT's Program; the
    Season axis is never consulted. ``season`` is here because a Season create
    MINTS the Season axis rather than consuming it — its parent is the Program.
  * SEASON-OWNED — league / division / game / registration (and league_season /
    season_venue_access, which have no create route of their own). Two-axis:
    an explicit saved Program AND Season, both compared against the parents.

THE ENFORCEMENT SHAPE, per surface:

  missing required context   -> stable 409 ``active_context_required``
  stale required axis        -> stable 409 ``active_context_required``
  different saved axes,
     REAL parent             -> generic 404, BYTE-IDENTICAL to a nonexistent id
  exact explicit axes        -> 200, the POSITIVE CONTROL

and every refusal writes NO entity, NO relationship, NO audit row — asserted by
snapshotting every table plus the audit count around each probe, so a refusal
that left counter-visible residue fails here rather than in production.

ORDERING. #369's parent-visibility gate runs BEFORE #409's on this family, the
reverse of the mutation family. Deciding "does this parent carry a Program"
needs a store read, so unlike ``_mutation_axis_targets`` the create refusal is
not lookup-free; answering 409 first would let a caller who has chosen nothing
separate "that id exists and is linked" (409) from "nonexistent or foreign"
(404), which is a new existence oracle. Past the visibility gate the caller
provably CAN already see the parent, so the 409 discloses nothing new.
``RefusalIsNotAnExistenceOracleTest`` pins the byte-identity that makes it so.

BOOTSTRAP is the constraint that shapes the whole rule. A parent that carries
NO axis has nothing to compare, so the create proceeds — that is what the
ruling's PROGRAM-AXIS clause literally says ("compare the SAVED Program against
the PARENT's Program"), and it is the only reading under which
``tests/test_zero_program_bootstrap_scoping.py`` (organization -> venue -> rink
-> ice-slot -> official over two real Arena Manager sessions on a Program-LESS
install, 200 on every one) still holds. ``BootstrapTest`` here pins the other
half: create Program -> explicit Program-ONLY selection -> the Program-owned
creates and links succeed before any Season exists.

THREE STORES. ``InMemoryStore``, SQLite and PostgreSQL. The decision reads an
``ActiveContext`` row under a lock and compares it to link triples walked out
of several tables — a dict lookup on one store, real queries with real NULL
semantics and real row locks on the others — so agreement between them is part
of the claim. A SKIP IS NOT A PASS: the PostgreSQL classes announce loudly when
``TEST_DATABASE_URL`` is unset.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.services import context_service as ctx_mod
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None

# Every store table the guard must leave untouched on a refusal, plus the audit
# count. Named explicitly rather than derived, so a new table has to be added
# here on purpose instead of silently escaping the residue assertion.
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
            "— the #409 CREATE family was NOT exercised on PostgreSQL. A SKIP "
            "HERE IS NOT A PASS: real row locks, real NULL semantics and the "
            "two-connection race are all unproven without it.")


class _CreateHarness:
    """Two Programs, each fully populated, and a fresh operator per test.

    ``setUp`` rebuilds the shared ``srv.STATE`` on the chosen backend for EVERY
    test. That is load-bearing rather than tidy: ``_fallback()``'s choice
    depends on which Programs and active Seasons exist, so a world inherited
    from an earlier case would make "the fallback lands on Program A" an
    accident of execution order instead of a controlled fixture.
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
        self.w = self._world()
        self._seq = 0

    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}-{uuid.uuid4().hex[:6]}"

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        """(status, raw_text, parsed). The RAW body is kept because the
        byte-identity of two refusals is the actual contract."""
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
        """A BRAND-NEW League Admin with NO persisted ActiveContext, signed in
        over real HTTP with a real session cookie.

        A fresh identity per case makes "nothing is selected" a property of the
        account rather than of execution order, and the assertion below makes
        every "no selection" claim below non-vacuous.
        """
        username = f"cr_{uuid.uuid4().hex[:10]}"
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

    def _select(self, c, program_id, season_id=None):
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        status, raw, resp = self._req(c, "POST", "/api/context", body)
        self.assertEqual(status, 200, raw)
        self.assertEqual((resp.get("program") or {}).get("id"), program_id, raw)
        return resp

    # -- fixture -----------------------------------------------------------
    def _world(self):
        """Programs A and B, each with a Season, a League, a Division, a Club
        (LINKED to its Program through a Team, so the Club really carries that
        Program's edge), a Team, a Venue carrying the legacy Program link, a
        Rink and an ice slot.

        Built through the setup service with ``actor_id`` only — no identity,
        so none of the #409 gates run. This is the FIXTURE, not the surface
        under test, and nobody has selected any of it.
        """
        svc = srv.STATE.api.setup
        w = {}
        for key, label in (("a", "Alpha"), ("b", "Bravo")):
            program = svc.create_program(f"{label} Program",
                                         timezone_name=TZ)
            season = svc.create_season(program.id, f"{label} Season")
            league = svc.create_league(season.id, f"{label} League")
            division = svc.create_division(season.id, f"{label} Div",
                                           league_id=league.id)
            club = svc.create_club(f"{label} Club")
            team = svc.create_team(club_id=club.id, name=f"{label} Team",
                                   program_id=program.id, league_id=league.id)
            team2 = svc.create_team(club_id=club.id, name=f"{label} Team 2",
                                    program_id=program.id, league_id=league.id)
            org = svc.create_organization(f"{label} Org")
            venue = svc.create_venue(f"{label} Venue",
                                     organization_id=org.id,
                                     league_id=program.id)
            rink = svc.create_rink(venue.id, f"{label} Rink")
            w[key] = {
                "program": program.id, "season": season.id,
                "league": league.id, "division": division.id,
                "club": club.id, "team": team.id, "team2": team2.id,
                "org": org.id, "venue": venue.id, "rink": rink.id,
            }
        return w

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
        """The refusal bytes with the echoed ids masked. Byte-for-byte equality
        of two refusals is the contract; the only legitimate difference is the
        id the caller itself supplied."""
        for index, value in enumerate(ids):
            if value:
                raw = raw.replace(value, f"<ID{index}>")
        return raw

    # -- the create matrix -------------------------------------------------
    #
    # ``(label, axis_class, method, path, body)`` where ``body`` is a callable
    # taking the world half ("a" or "b") plus a unique suffix. Every entry
    # names at least one axis-bearing parent, so every entry is gate-reachable.
    def _cases(self):
        w = self.w

        def season(half, tag):
            return ("POST", "/api/v2/setup/season",
                    {"program_id": w[half]["program"], "name": tag})

        def venue_v1(half, tag):
            return ("POST", "/api/setup/venue",
                    {"name": tag, "league_id": w[half]["program"]})

        def rink(half, tag):
            return ("POST", "/api/v2/setup/rink",
                    {"venue_id": w[half]["venue"], "name": tag})

        def ice_slot(half, tag):
            # A distinct DAY per call: two slots on one rink may not overlap,
            # and this builder is invoked many times per test.
            day = 1 + (self._seq % 27)
            return ("POST", "/api/v2/setup/ice-slot",
                    {"rink_id": w[half]["rink"],
                     "start_time": f"2031-01-{day:02d}T18:00:00+00:00",
                     "end_time": f"2031-01-{day:02d}T19:30:00+00:00"})

        def official(half, tag):
            return ("POST", "/api/v2/setup/official",
                    {"name": tag, "home_club_id": w[half]["club"]})

        def team_v1(half, tag):
            return ("POST", "/api/setup/team",
                    {"club_id": w[half]["club"], "name": tag,
                     "league_id": w[half]["program"],
                     "division_id": w[half]["division"]})

        def player(half, tag):
            return ("POST", "/api/v2/setup/player",
                    {"team_id": w[half]["team"], "name": tag,
                     "position": "forward"})

        def league(half, tag):
            return ("POST", "/api/v2/setup/league",
                    {"season_id": w[half]["season"], "name": tag})

        def division(half, tag):
            return ("POST", "/api/v2/setup/division",
                    {"league_id": w[half]["league"],
                     "season_id": w[half]["season"], "name": tag})

        def registration(half, tag):
            return ("POST",
                    f"/api/v2/setup/seasons/{w[half]['season']}"
                    "/team-registrations",
                    {"team_id": w[half]["team2"],
                     "league_id": w[half]["league"]})

        # ``sole`` marks a case whose request names exactly ONE axis-bearing
        # parent. Only those can be compared BYTE-FOR-BYTE against a
        # nonexistent id: with two or more, the refusal legitimately names
        # whichever parent it reached first, so swapping one id for a ghost
        # changes WHICH question is being answered rather than the answer.
        return [
            ("season", "program", season, ("program", "program"), True),
            ("venue(v1 league_id)", "program", venue_v1,
             ("program", "program"), True),
            ("rink", "program", rink, ("venue", "venue"), True),
            ("ice_slot", "program", ice_slot, ("rink", "rink"), True),
            ("official", "program", official, ("club", "club"), True),
            ("team(v1)", "program", team_v1, ("club", "club"), False),
            ("player", "program", player, ("team", "team"), True),
            ("league", "season", league, ("season", "season"), True),
            ("division", "season", division, ("season", "season"), False),
            ("registration", "season", registration, ("season", "season"),
             False),
        ]

    # -- reachable stale states -------------------------------------------
    def _make_stale_program(self, c):
        """The operator's saved row names a Program THAT NO LONGER EXISTS,
        reached through four ordinary authenticated calls and no direct store
        access: create a fresh empty Program, select it, delete it.

        The premise is asserted, not narrated: the saved row must SURVIVE the
        Program it names, or this collapses into a second copy of the
        no-selection state and stops being about staleness."""
        status, raw, program = self._req(
            c, "POST", "/api/v2/setup/program",
            {"name": self._next("Stale Program"), "timezone": TZ})
        self.assertEqual(status, 200, raw)
        self._select(c, program["id"])
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/program/{program['id']}/delete", {})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(self.store.get_program(program["id"]),
                          "the Program survived its own delete — the saved "
                          "selection is not stale and this proves nothing")
        return program["id"]

    def _make_stale_season(self, c):
        """The operator's saved row names a Program that is FINE and a Season
        that no longer exists — the shape that must be refused by the two-axis
        rule and ADMITTED by the Program-axis one."""
        status, raw, program = self._req(
            c, "POST", "/api/v2/setup/program",
            {"name": self._next("Half Stale"), "timezone": TZ})
        self.assertEqual(status, 200, raw)
        self._select(c, program["id"])
        status, raw, season = self._req(
            c, "POST", "/api/v2/setup/season",
            {"program_id": program["id"], "name": "Doomed"})
        self.assertEqual(status, 200, raw)
        self._select(c, program["id"], season["id"])
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/season/{season['id']}/delete", {})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(self.store.get_season(season["id"]),
                          "the Season survived its own delete — the saved "
                          "selection is not stale and this proves nothing")
        return program["id"], season["id"]

    def _assert_saved_row_still_names(self, user_id, program_id,
                                      season_id=None):
        saved = self.store.get_active_context(user_id)
        self.assertIsNotNone(saved, "the saved row vanished with its target")
        self.assertEqual(saved.program_id, program_id, "saved Program moved")
        if season_id is not None:
            self.assertEqual(saved.season_id, season_id, "saved Season moved")


# ---------------------------------------------------------------------------
# 1. ZERO-AXIS ROOTS: no context, and PROVABLY no context READ.
# ---------------------------------------------------------------------------
class _ZeroAxisRootMixin(_CreateHarness):
    """organization / program / club succeed with NO saved selection, and the
    proof is a spy on ``ContextService._fallback`` recording ZERO calls — not
    merely a 200.

    The spy wraps ``_fallback`` itself and NOT ``resolve``/
    ``resolve_with_league``, because ``_resolve_locked`` honours a valid saved
    row without ever reaching ``_fallback``: a zero-call assertion on the
    resolver would pass for the wrong reason. The measured baseline for a
    currently-broken path is 1 call (the ``official`` falsification in this
    module's docstring), so zero here is a real distinction.
    """

    def _spy(self):
        calls = []
        original = ctx_mod.ContextService._fallback

        def spy(inner_self, *args, **kwargs):
            calls.append(args[:1])
            return original(inner_self, *args, **kwargs)

        ctx_mod.ContextService._fallback = spy
        self.addCleanup(setattr, ctx_mod.ContextService, "_fallback", original)
        return calls

    def test_zero_axis_roots_create_with_no_context_and_no_fallback_call(self):
        c, user_id = self._operator()
        calls = self._spy()
        created = {}
        for entity, body in (
                ("organization", {"name": self._next("Root Org"),
                                  "short_name": "RO"}),
                ("program", {"name": self._next("Root Program"),
                             "timezone": TZ}),
                ("club", {"name": self._next("Root Club"),
                          "country": "CA"})):
            status, raw, resp = self._req(
                c, "POST", f"/api/v2/setup/{entity}", body)
            self.assertEqual(
                status, 200,
                f"a ZERO-AXIS ROOT create was refused: {entity} -> {raw}")
            created[entity] = resp["id"]
            self.assertEqual(
                calls, [],
                f"creating `{entity}` consulted ContextService._fallback "
                f"{len(calls)} time(s). A root create must read NO context at "
                "all — a fallback that runs here is a Program nobody chose "
                "being brought into the decision")
        self.assertIsNone(
            self.store.get_active_context(user_id),
            "a root create persisted a selection as a side effect")
        # ...and the v1 twins take the same path, so v1 is not a second policy.
        for entity, body in (
                ("organization", {"name": self._next("V1 Root Org")}),
                ("league", {"name": self._next("V1 Root Program"),
                            "timezone": TZ}),
                ("club", {"name": self._next("V1 Root Club")})):
            status, raw, _ = self._req(c, "POST", f"/api/setup/{entity}", body)
            self.assertEqual(status, 200, (entity, raw))
        self.assertEqual(
            calls, [],
            f"the v1 root creates consulted _fallback {len(calls)} time(s)")
        self.assertTrue(created["program"].startswith("league_"), created)


# ---------------------------------------------------------------------------
# 2. THE FALSIFIED PATH, closed.
# ---------------------------------------------------------------------------
class _FalsifiedOfficialMixin(_CreateHarness):
    """The exact create the falsification measured: a signed-in League Admin
    whose saved ``ActiveContext`` row is ABSENT, homing a new Official at a
    Club that carries Program A's edge.

    Before the fix this returned 200 and wrote the Official plus its audit row,
    authorized by the single ``_fallback()`` call
    ``setup_parent_writable`` makes. It must now refuse, and refuse with
    NOTHING written.
    """

    def test_the_measured_official_create_is_refused_with_no_residue(self):
        c, user_id = self._operator()
        self.assertIsNone(self.store.get_active_context(user_id))
        # The premise the falsification rests on: the Club really does carry
        # Program A's edge, so this create really would have landed inside A.
        edges, saw_link = srv.STATE.api._setup_target_edges(
            "club", self.store.get_club(self.w["a"]["club"]))
        self.assertTrue(saw_link, "fixture: A-Club is not linked at all")
        self.assertEqual({e[0] for e in edges}, {self.w["a"]["program"]},
                         "fixture: A-Club does not carry Program A's edge")

        before = self._snapshot()
        status, raw, resp = self._req(
            c, "POST", "/api/v2/setup/official",
            {"name": "Falsified", "home_club_id": self.w["a"]["club"]})
        self.assertEqual(
            status, 409,
            "an Official was created inside a Program the operator never "
            f"chose: {raw}")
        self.assertEqual(resp["error"]["code"], "active_context_required", raw)
        self._assert_no_residue(before, "official create with no selection")


# ---------------------------------------------------------------------------
# 3. THE FOUR-STATE MATRIX, per create kind.
# ---------------------------------------------------------------------------
class _CreateMatrixMixin(_CreateHarness):
    """For every create kind: missing context -> 409, stale required axis ->
    409, different saved axes with a REAL parent -> generic 404, exact axes ->
    200. Each refusal is additionally required to leave zero residue."""

    def test_missing_context_refuses_every_create(self):
        for label, axis, build, _parent, _sole in self._cases():
            with self.subTest(create=label, axis=axis):
                c, user_id = self._operator()
                method, path, body = build("a", self._next(label))
                before = self._snapshot()
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 409,
                    f"`{label}` was created with NO chosen context: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / no selection")
                self.assertIsNone(self.store.get_active_context(user_id))

    def test_a_stale_program_axis_refuses_every_create(self):
        for label, axis, build, _parent, _sole in self._cases():
            with self.subTest(create=label, axis=axis):
                c, user_id = self._operator()
                stale_program = self._make_stale_program(c)
                self._assert_saved_row_still_names(user_id, stale_program)
                method, path, body = build("a", self._next(label))
                before = self._snapshot()
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 409,
                    f"`{label}` was created against a saved Program that no "
                    f"longer exists: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / stale Program")

    def test_a_stale_season_axis_refuses_only_the_season_owned_creates(self):
        """The asymmetry is the #409 rule, not an accident: a mutation that
        does not CONSUME the Season axis must not be refused for the state of
        one, or a Program whose last Season was deleted becomes unusable."""
        for label, axis, build, _parent, _sole in self._cases():
            with self.subTest(create=label, axis=axis):
                c, user_id = self._operator()
                program, season = self._make_stale_season(c)
                self._assert_saved_row_still_names(user_id, program, season)
                # The saved Program is a DIFFERENT (fresh) Program, so aim the
                # create at it — otherwise this would be measuring the
                # cross-Program 404 instead of the stale-Season 409.
                if axis != "season":
                    continue
                method, path, body = build("a", self._next(label))
                before = self._snapshot()
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 409,
                    f"the SEASON-OWNED create `{label}` was accepted against "
                    f"a saved Season that no longer exists: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / stale Season")

    def test_a_different_saved_context_answers_a_generic_not_found(self):
        """A REAL parent in Program A, with Program B explicitly selected, must
        answer BYTE-IDENTICALLY to a parent id that never existed — otherwise
        200-vs-404 is an existence oracle over another Program's records."""
        for label, axis, build, (parent_kind, parent_key), sole in self._cases():
            with self.subTest(create=label, axis=axis):
                c, _user_id = self._operator()
                self._select(c, self.w["b"]["program"], self.w["b"]["season"])
                method, path, body = build("a", self._next(label))
                before = self._snapshot()
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 404,
                    f"`{label}` named a Program-A parent while Program B was "
                    f"selected and was not refused generically: {raw}")
                self.assertEqual(resp["error"]["code"], "not_found", raw)
                self._assert_no_residue(before, f"{label} / foreign parent")

                # The refusal may echo only ids the CALLER itself supplied.
                supplied = set(body.values()) | {path}
                for secret in (self.w["a"]["program"], self.w["a"]["season"],
                               self.w["a"]["league"]):
                    if not any(secret in str(v) for v in supplied):
                        self.assertNotIn(
                            secret, raw,
                            f"`{label}`: the refusal names {secret}, which "
                            "this caller never sent and may not see")

                if not sole:
                    continue
                # BYTE-IDENTITY, for the cases whose request names exactly one
                # axis-bearing parent: the SAME request with that id replaced
                # by one that never existed must be indistinguishable.
                real_id = self.w["a"][parent_key]
                ghost = f"{parent_kind}_never_existed"
                ghost_method, ghost_path, ghost_body = build(
                    "a", self._next(label))
                ghost_path = ghost_path.replace(real_id, ghost)
                ghost_body = {k: (ghost if v == real_id else v)
                              for k, v in ghost_body.items()}
                gstatus, graw, _ = self._req(
                    c, ghost_method, ghost_path, ghost_body)
                self.assertEqual(
                    (gstatus, self._blind(graw, ghost)),
                    (status, self._blind(raw, real_id)),
                    f"`{label}`: an inaccessible parent and a nonexistent one "
                    f"answer differently, which is an existence oracle "
                    f"({raw} vs {graw})")

    def test_the_exact_explicit_axes_succeed(self):
        """THE POSITIVE CONTROL. Without it every refusal above is vacuous — a
        route that refused everything would satisfy all three."""
        for label, axis, build, _parent, _sole in self._cases():
            with self.subTest(create=label, axis=axis):
                c, _user_id = self._operator()
                self._select(c, self.w["a"]["program"], self.w["a"]["season"])
                method, path, body = build("a", self._next(label))
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused under its OWN exact axes: {raw}")
                self.assertNotIn("error", resp, raw)

    def test_program_axis_creates_survive_a_missing_or_dangling_season(self):
        """A Program-axis create consumes no Season, so a Program-ONLY
        selection and a saved row whose Season was DELETED must both work.

        This is the half of the rule that makes it a rule rather than a
        tightening: demanding a Season here would make a Program whose last
        Season was just deleted permanently unable to grow a Venue, a Team or
        its next Season."""
        program_axis = [case for case in self._cases() if case[1] == "program"]
        self.assertTrue(program_axis, "the matrix lost its Program-axis half")

        for label, _axis, build, _parent, _sole in program_axis:
            with self.subTest(create=label, season="never selected"):
                c, _user_id = self._operator()
                self._select(c, self.w["a"]["program"])       # Program ONLY
                method, path, body = build("a", self._next(label))
                status, raw, _ = self._req(c, method, path, body)
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused under a Program-ONLY selection, "
                    f"an axis it does not consume: {raw}")

        for label, _axis, build, _parent, _sole in program_axis:
            with self.subTest(create=label, season="deleted under the row"):
                c, user_id = self._operator()
                # A fresh EMPTY Season of Program A, selected and then deleted:
                # the saved row keeps naming Program A and dangles on a Season.
                # The Season create is itself PROGRAM-AXIS, so stand in A first.
                self._select(c, self.w["a"]["program"])
                status, raw, season = self._req(
                    c, "POST", "/api/v2/setup/season",
                    {"program_id": self.w["a"]["program"],
                     "name": self._next("Doomed A")})
                self.assertEqual(status, 200, raw)
                self._select(c, self.w["a"]["program"], season["id"])
                status, raw, _ = self._req(
                    c, "POST", f"/api/v2/setup/season/{season['id']}/delete",
                    {})
                self.assertEqual(status, 200, raw)
                self._assert_saved_row_still_names(
                    user_id, self.w["a"]["program"], season["id"])
                self.assertIsNone(self.store.get_season(season["id"]))
                method, path, body = build("a", self._next(label))
                status, raw, _ = self._req(c, method, path, body)
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused because the saved row's SEASON "
                    f"dangled, an axis it does not consume: {raw}")


# ---------------------------------------------------------------------------
# 4. BOOTSTRAP.
# ---------------------------------------------------------------------------
class _BootstrapMixin(_CreateHarness):
    """create Program -> explicit Program-ONLY selection -> the Program-owned
    creates and links succeed BEFORE any Season exists.

    The zero-Program half of the bootstrap is pinned by
    ``tests/test_zero_program_bootstrap_scoping.py`` (two real Arena Manager
    sessions on an install with no Program at all). This is the half above it:
    once a Program exists and has been chosen, everything that hangs off the
    Program rather than off a Season must be reachable with no Season selected,
    because there is not one yet."""

    def test_a_fresh_program_can_be_populated_before_it_has_a_season(self):
        c, user_id = self._operator()
        status, raw, program = self._req(
            c, "POST", "/api/v2/setup/program",
            {"name": self._next("Bootstrap"), "timezone": TZ})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(
            self.store.get_active_context(user_id),
            "the root create selected a context for the operator — the "
            "explicit Program-only selection below would then be vacuous")

        self._select(c, program["id"])                    # Program ONLY
        saved = self.store.get_active_context(user_id)
        self.assertEqual((saved.program_id, saved.season_id),
                         (program["id"], None),
                         "the fixture did not achieve a Program-ONLY row")
        self.assertEqual(
            [s for s in self.store.all_seasons()
             if s.program_id == program["id"]], [],
            "the bootstrap Program already has a Season, so 'pre-Season' "
            "proves nothing")

        # Program-owned creates and the links between them, with no Season.
        status, raw, org = self._req(c, "POST", "/api/v2/setup/organization",
                                     {"name": self._next("BS Org")})
        self.assertEqual(status, 200, raw)
        status, raw, club = self._req(c, "POST", "/api/v2/setup/club",
                                      {"name": self._next("BS Club")})
        self.assertEqual(status, 200, raw)
        # The legacy v1 Venue->Program link IS the Program comparison, made
        # against the Program just chosen.
        status, raw, venue = self._req(
            c, "POST", "/api/setup/venue",
            {"name": self._next("BS Venue"), "league_id": program["id"]})
        self.assertEqual(status, 200, raw)
        status, raw, rink = self._req(
            c, "POST", "/api/v2/setup/rink",
            {"venue_id": venue["id"], "name": self._next("BS Rink")})
        self.assertEqual(status, 200, raw)
        status, raw, slot = self._req(
            c, "POST", "/api/v2/setup/ice-slot",
            {"rink_id": rink["id"],
             "start_time": "2032-02-02T18:00:00+00:00",
             "end_time": "2032-02-02T19:30:00+00:00"})
        self.assertEqual(status, 200, raw)
        status, raw, official = self._req(
            c, "POST", "/api/v2/setup/official",
            {"name": self._next("BS Official"), "home_club_id": club["id"]})
        self.assertEqual(status, 200, raw)
        # ...and the first Season becomes creatable under the SAME
        # Program-only selection, which is the step that would be impossible
        # if a Season create consumed the Season axis it MINTS.
        # (A route-created Team needs a permanent League, which needs a Season,
        # so the Team link is exercised after the Season below rather than
        # here — see `team_league_required` in the setup service.)
        status, raw, season = self._req(
            c, "POST", "/api/v2/setup/season",
            {"program_id": program["id"], "name": "BS Season"})
        self.assertEqual(
            status, 200,
            "the FIRST Season of a freshly chosen Program was refused, which "
            f"would make the Program permanently Season-less: {raw}")
        self.assertEqual(self.store.get_season(season["id"]).program_id,
                         program["id"], raw)
        self.assertTrue(all(x.get("id") for x in
                            (org, club, venue, rink, slot, official)))


# ---------------------------------------------------------------------------
# 5. The refusal is not an existence oracle.
# ---------------------------------------------------------------------------
class _OracleMixin(_CreateHarness):
    """#369's parent-visibility gate runs FIRST on this family, so the 409 is
    only ever reachable for a parent the caller can already see. This pins the
    consequence: a caller with NO selection cannot use the 409-vs-404 split to
    learn which parent ids exist."""

    def test_an_unselected_caller_learns_nothing_from_the_refusal_code(self):
        c, _user_id = self._operator()
        # A parent that exists and is visible (the fallback's own Program is
        # what makes it visible — which is exactly the authorization #409
        # removes) answers 409...
        status_real, raw_real, _ = self._req(
            c, "POST", "/api/v2/setup/official",
            {"name": "probe", "home_club_id": self.w["a"]["club"]})
        self.assertEqual(status_real, 409, raw_real)
        # ...and a nonexistent one answers the generic 404 the facade itself
        # emits for a missing Club, with no new information in it.
        status_ghost, raw_ghost, ghost = self._req(
            c, "POST", "/api/v2/setup/official",
            {"name": "probe", "home_club_id": "club_never_existed"})
        self.assertEqual(status_ghost, 404, raw_ghost)
        self.assertEqual(ghost["error"]["message"],
                         "Club club_never_existed not found.", raw_ghost)
        # The 409 body must not name the Program, Season or Club it refused
        # against — it is decided from the KIND and from whether the parent
        # carries an axis, never from WHICH axis it carries.
        for secret in (self.w["a"]["program"], self.w["a"]["season"],
                       self.w["a"]["league"]):
            self.assertNotIn(secret, raw_real,
                             f"the 409 body names {secret}, which the caller "
                             "was never told they may see")


# ---------------------------------------------------------------------------
# Concrete backends.
# ---------------------------------------------------------------------------
class ZeroAxisRootMemoryTest(_ZeroAxisRootMixin, unittest.TestCase):
    DATABASE_URL = None


class ZeroAxisRootSqliteTest(_ZeroAxisRootMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class ZeroAxisRootPostgresTest(_ZeroAxisRootMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


class FalsifiedOfficialMemoryTest(_FalsifiedOfficialMixin, unittest.TestCase):
    DATABASE_URL = None


class FalsifiedOfficialSqliteTest(_FalsifiedOfficialMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class FalsifiedOfficialPostgresTest(_FalsifiedOfficialMixin,
                                    unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


class CreateMatrixMemoryTest(_CreateMatrixMixin, unittest.TestCase):
    DATABASE_URL = None


class CreateMatrixSqliteTest(_CreateMatrixMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class CreateMatrixPostgresTest(_CreateMatrixMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


class BootstrapMemoryTest(_BootstrapMixin, unittest.TestCase):
    DATABASE_URL = None


class BootstrapSqliteTest(_BootstrapMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class BootstrapPostgresTest(_BootstrapMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


class OracleMemoryTest(_OracleMixin, unittest.TestCase):
    DATABASE_URL = None


class OracleSqliteTest(_OracleMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class OraclePostgresTest(_OracleMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


# ---------------------------------------------------------------------------
# 6. The two-connection context-switch/create race — PostgreSQL only.
# ---------------------------------------------------------------------------
class PostgresCreateSwitchRaceTest(unittest.TestCase):
    """Only the CONTEXT-CONSISTENT outcome may commit.

    THE HAZARD. ``_create_context_error`` reads the operator's saved
    ActiveContext row and then the create writes. If a concurrent
    ``POST /api/context`` could commit a DIFFERENT tuple in between, the create
    would land authorized by a tuple that was never in effect when the decision
    was taken — the check/use gap #386 closed for the scheduler and #410 for
    the ice commit.

    THE CLAIM. ``setup_guarded_create`` takes the ActiveContext ROW LOCK as the
    FIRST statement of the transaction that performs the create
    (``_explicit_selection(lock=True)`` -> ``get_active_context_for_update`` ->
    ``pg_advisory_xact_lock`` on the per-user key), which is the same key
    ``set_active_context`` takes. So a switch either orders wholly BEFORE the
    decision or waits for the create it would have authorized.

    WHY TWO CONNECTIONS. ``SqlStore`` holds exactly one connection and its
    ``transaction()`` holds a per-instance ``RLock`` for the whole block, so two
    concurrent requests to one server never hold two open PostgreSQL
    transactions — the second blocks on a Python lock having never reached the
    database. The switcher here therefore runs on a SEPARATE ``SqlStore`` with
    its own backend, which is the only shape in which the advisory lock can be
    the mechanism that orders them.

    NOTHING TOUCHES ``self.store`` WHILE A IS PAUSED: A's request thread holds
    that store's RLock, so a stray read inside the pause window would hang the
    suite rather than fail it. Every observation during the pause goes through
    ``store_b``.
    """

    def setUp(self):
        url = _postgres_url()
        if not url:
            raise unittest.SkipTest(_PG_SKIP)
        self.saved_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        srv.STATE.reset(seed=False)
        self.store = srv.STATE.api.store
        svc = srv.STATE.api.setup
        self.program_a = svc.create_program("Race A", timezone_name=TZ).id
        self.season_a = svc.create_season(self.program_a, "Race A S").id
        self.program_b = svc.create_program("Race B", timezone_name=TZ).id
        self.season_b = svc.create_season(self.program_b, "Race B S").id
        account = srv.STATE.api.accounts.create_account(
            f"race_{uuid.uuid4().hex[:8]}", "demo", Role.LEAGUE_ADMIN)
        self.user_id = account.id
        self.username = account.username

        from hockey_scheduler.store import create_store
        self.store_b = create_store(url)
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            close = getattr(self.store_b, "close", None)
            if close:
                close()
        except Exception:
            pass
        if self.saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.saved_url

    # -- HTTP --------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
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

    def _login(self):
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": self.username,
                                    "password": "demo"})
        self.assertEqual(status, 200, raw)
        return c

    # -- the pause seam ----------------------------------------------------
    def _pause(self, seconds, when):
        """Block A at a chosen point of its own create, by wrapping the LIVE
        ``ApiService._explicit_selection`` on the running service instance.

        No production file is touched. ``when="boundary"`` blocks after the
        LOCKING call, i.e. 'A holds the per-user ActiveContext lock and has not
        committed'. ``when="preflight"`` blocks after the UNLOCKED call, i.e.
        'A has passed the cheap preflight and has not yet reached the
        boundary' — the exact window in which a stale preflight answer would
        become a wrong authorization if the preflight were the boundary.
        """
        api = srv.STATE.api
        original = api._explicit_selection
        reached = threading.Event()
        want_lock = (when == "boundary")

        def paused(user_id, role, scope, lock=False):
            result = original(user_id, role, scope, lock=lock)
            if lock is want_lock and user_id == self.user_id:
                reached.set()
                time.sleep(seconds)
            return result

        api._explicit_selection = paused
        self.addCleanup(setattr, api, "_explicit_selection", original)
        return reached

    def _race(self, connection_a, connection_b):
        """Run the two sides and re-raise anything either thread raised — an
        assertion that fires inside a worker thread must not be swallowed into
        a green run."""
        errors = {}

        def guard(name, fn):
            def run():
                try:
                    fn()
                except BaseException as exc:      # re-raised below
                    errors[name] = exc
            return run

        thread_a = threading.Thread(target=guard("A", connection_a))
        thread_b = threading.Thread(target=guard("B", connection_b))
        thread_a.start()
        thread_b.start()
        thread_a.join(30)
        thread_b.join(30)
        self.assertFalse(thread_a.is_alive(), "connection A never returned")
        self.assertFalse(thread_b.is_alive(), "connection B never returned")
        for name, exc in errors.items():
            raise AssertionError(f"connection {name} failed: {exc!r}")

    def test_a_switch_cannot_land_inside_the_guarded_create(self):
        """A holds the per-user ActiveContext lock for the whole create, so B's
        switch either orders wholly before the decision or waits for the write
        it would have authorized. Only the context-consistent outcome commits:
        A's League is bound into the Season A had CHOSEN, never the one B
        switched to."""
        c = self._login()
        status, raw, _ = self._req(c, "POST", "/api/context",
                                   {"program_id": self.program_a,
                                    "season_id": self.season_a})
        self.assertEqual(status, 200, raw)

        reached = self._pause(1.5, "boundary")
        result = {}

        def connection_a():
            result["response"] = self._req(
                c, "POST", "/api/v2/setup/league",
                {"season_id": self.season_a, "name": "Race League"})

        def connection_b():
            # A SECOND real PostgreSQL backend, taking the SAME per-user
            # advisory key `set_active_context` always takes.
            self.assertTrue(reached.wait(10), "A never reached the pause seam")
            self.store_b.set_active_context(ActiveContext(
                id=self.user_id, program_id=self.program_b,
                season_id=self.season_b,
                updated_at=datetime.now(timezone.utc)))

        self._race(connection_a, connection_b)

        status, raw, resp = result["response"]
        self.assertEqual(status, 200, f"A's create was lost to the race: {raw}")
        bindings = [ls for ls in self.store.all_league_seasons()
                    if ls.league_id == resp["id"]]
        self.assertEqual([ls.season_id for ls in bindings], [self.season_a],
                         "A's League was bound into a Season nobody chose")
        saved = self.store.get_active_context(self.user_id)
        self.assertEqual((saved.program_id, saved.season_id),
                         (self.program_b, self.season_b),
                         "B's switch never landed, so nothing was raced")

    def test_a_switch_between_the_preflight_and_the_boundary_refuses(self):
        """THE CHECK/USE CLOSURE, proven rather than asserted.

        A's cheap preflight passes under (A, SA). B then switches the SAME
        user's saved row to (B, SB) — on a second connection, while A is parked
        between its preflight and its transaction. If the preflight were the
        boundary, A would go on to create a League inside Season A under a
        context that no longer says so. It must instead be re-decided under the
        lock and REFUSED, with nothing written."""
        c = self._login()
        status, raw, _ = self._req(c, "POST", "/api/context",
                                   {"program_id": self.program_a,
                                    "season_id": self.season_a})
        self.assertEqual(status, 200, raw)

        reached = self._pause(1.5, "preflight")
        result = {}
        leagues_before = {lg.id for lg in self.store.all_leagues()}
        bindings_before = {ls.id for ls in self.store.all_league_seasons()}
        audit_before = len(self.store.all_setup_audit())

        def connection_a():
            result["response"] = self._req(
                c, "POST", "/api/v2/setup/league",
                {"season_id": self.season_a, "name": "Preflight Race"})

        def connection_b():
            self.assertTrue(reached.wait(10),
                            "A never reached the PREFLIGHT pause seam, so the "
                            "check/use window was never opened")
            self.store_b.set_active_context(ActiveContext(
                id=self.user_id, program_id=self.program_b,
                season_id=self.season_b,
                updated_at=datetime.now(timezone.utc)))

        self._race(connection_a, connection_b)

        status, raw, _ = result["response"]
        self.assertEqual(
            status, 404,
            "the create COMMITTED against a context that had already been "
            f"switched away — the preflight was treated as the boundary: {raw}")
        self.assertEqual({lg.id for lg in self.store.all_leagues()},
                         leagues_before, "the refused create wrote a League")
        self.assertEqual({ls.id for ls in self.store.all_league_seasons()},
                         bindings_before,
                         "the refused create wrote a LeagueSeason binding")
        self.assertEqual(len(self.store.all_setup_audit()), audit_before,
                         "the refused create wrote an audit row")


if __name__ == "__main__":
    unittest.main()
