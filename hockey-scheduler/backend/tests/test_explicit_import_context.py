"""A NAME OR EXTERNAL REFERENCE IS STILL IDENTITY (#411, the import family).

THE DEFECT. ``/api/import/commit/officials-availability`` and
``/api/import/commit/rinks-ice-slots`` reached their write through
``_send_api`` with NO ``_guarded_create``, no ``setup_create_context_error``
and no ``ActiveContext`` read anywhere on the path. They had been classified
axis-free because the payload carries no parent id — and that reasoning is
wrong for every REPEAT import:

  * ``officials[].official_code`` resolves an EXISTING Official by
    ``external_ref`` and ``officials[].home_club_name`` an EXISTING Club by
    exact name;
  * ``rinks[].rink_code`` resolves an EXISTING Rink by ``external_ref`` and
    ``rinks[].venue_name`` an EXISTING Venue by exact name;
  * ``official_availability[].official_code`` is admitted DELIBERATELY against
    officials persisted by a PRIOR commit, so a payload carrying nothing but a
    code writes onto an existing Official.

Those resolved rows already carry a Program axis, and the commits then UPDATE
and REPARENT them (``official.home_club_id`` and ``rink.venue_id`` are both
overwritten) and hang contacts, availability windows, ice slots, an import
batch and audit rows off them. Measured on this branch before the fix, with a
signed-in League Admin whose saved ``ActiveContext`` row was asserted ABSENT::

    POST /api/import/commit/officials-availability
      officials_csv: "official_code,name,home_club_name\\nX,X,Alpha Club"
      -> 200 {"committed": true, ...}
      official edges after: {('league_1', None, 'league_2')}   # Program A

i.e. a durable write, plus its audit trail, into a Program the operator never
chose — reached with a NAME instead of an id.

THE RULE, in ``ApiService._import_context_error`` / ``setup_guarded_import``.
The split is on the RESOLUTION, never on the payload shape:

  nothing in the payload resolves        -> genuinely axis-free, and NO
                                            context is read at all
  a key resolves to an UNLINKED row      -> nothing to compare; proceeds (the
                                            same clause
                                            ``_create_parent_is_axis_free``
                                            already applies, and the only
                                            reading a Program-LESS install
                                            survives)
  a key resolves to a Program-bearing row-> PROGRAM-AXIS. No Season is
                                            demanded: facility Season edges
                                            are ACCESS GRANTS and an Official
                                            outlives every Season.

and the enforcement shape, per surface:

  missing saved Program        -> stable 409 ``active_context_required``
  STALE saved Program          -> the SAME stable 409, byte for byte
  keys SPANNING Programs       -> the SAME stable 409 (never a guess)
  a key LINKED but UNJUDGEABLE -> the SAME stable 409
  a key in ANOTHER Program     -> generic 404 ``"<Label> <key> not found."``
  a dangling pure REFERENCE    -> the SAME generic 404, byte for byte
  the exact saved Program      -> 200, THE POSITIVE CONTROL

Every refusal is required to write zero officials, clubs, contacts,
availability windows, venues, rinks, ice slots, import-batch rows and audit
entries — snapshotted around every probe, so residue fails here rather than in
production.

WHAT THE ORDERING BUYS. The 409 is decided before any key's existence is
confirmed or denied AND before the commits' own store-backed passes run.
``validate_official_availability`` computes "Unknown official_code {code}" from
``store.all_officials()`` across the WHOLE install, which was an existence
oracle over every Program's officials emitted before any context decision
existed; ``validate_import(store=...)`` resolves the Program-scope scheduling
policy through ``venue.league_id`` and reads every Program's ice slots and
games, and the booked-slot and overlap gates name an existing slot id back to
the caller. All of it now sits behind the gate — which
``ReferenceOracleMixin`` measures directly by requiring an unresolvable
``official_code`` and a real Program-A one to answer the SAME BYTES in both
the unselected and the wrong-Program states.

WHAT IS DELIBERATELY NOT CLAIMED, and is reported rather than papered over: a
key that resolves to NOTHING on the ``officials`` / ``rinks`` sheets is a
genuine CREATE and still proceeds, because that IS the pilot-onboarding
bootstrap. So for those create-or-match keys "this code is taken by another
Program" stays distinguishable from "this code is free". Refusing it would
make new-official and new-rink import impossible for any operator who has
chosen a Program, and migration 047/048's GLOBAL unique indexes on
``Official.external_ref`` / ``Rink.external_ref`` already make "taken"
observable from this surface no matter how the gate is written. What the gate
guarantees is that it can never be observed by WRITING into the row that holds
it — which is the property ``ForeignKeyMixin`` asserts, with a full residue
sweep, for every axis-bearing key of both endpoints.

THREE STORES. ``InMemoryStore``, SQLite and PostgreSQL. The decision reads an
``ActiveContext`` row under a lock and compares it against link triples walked
out of several tables — a dict lookup on one store, real queries with real
NULL semantics and real row locks on the others. A SKIP IS NOT A PASS: the
PostgreSQL classes announce loudly when ``TEST_DATABASE_URL`` is unset, and
the two-connection context-switch/import race exists only there.
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

OFFICIALS_PATH = "/api/import/commit/officials-availability"
RINKS_PATH = "/api/import/commit/rinks-ice-slots"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None

# Every store table these two commits can write, plus the audit trail. Named
# explicitly rather than derived, so a new table has to be added here on
# purpose instead of silently escaping the residue assertion. Availability
# windows and the import batch have no whole-table getter: availability is
# swept per official below, and an import batch's only persisted trace IS its
# ``import_committed`` audit row, which the audit snapshot covers.
_TABLES = (
    ("programs", "all_programs"), ("seasons", "all_seasons"),
    ("leagues", "all_leagues"), ("league_seasons", "all_league_seasons"),
    ("divisions", "all_divisions"), ("teams", "all_teams"),
    ("clubs", "all_clubs"), ("officials", "all_officials"),
    ("venues", "all_venues"), ("rinks", "all_rinks"),
    ("ice_slots", "all_ice_slots"),
    ("organizations", "all_organizations"),
    ("contact_destinations", "all_contact_destinations"),
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
            "— the #411 IMPORT family was NOT exercised on PostgreSQL. A SKIP "
            "HERE IS NOT A PASS: real row locks, real NULL semantics and the "
            "two-connection context-switch/import race are all unproven "
            "without it.")


class _ImportHarness:
    """Two Programs, each with a Club, a Venue, an Official carrying an
    ``external_ref`` and a Rink carrying one — i.e. exactly the persisted rows a
    REPEAT import's lookup keys resolve to — and a fresh operator per test.

    The Official and the Rink are minted THROUGH THE VERY COMMITS UNDER TEST,
    called at the facade with ``role=None`` (the ungated internal form the
    seeds and harnesses use). That is deliberate: it makes the fixture the same
    shape a real pilot's first import leaves behind, so the "repeat import"
    this module is about is a genuine second call rather than a hand-built
    approximation of one.
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
        over real HTTP with a real session cookie."""
        username = f"imp_{uuid.uuid4().hex[:10]}"
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
        api = srv.STATE.api
        svc = api.setup
        w = {}
        for key, label in (("a", "Alpha"), ("b", "Bravo")):
            program = svc.create_program(f"{label} Program", timezone_name=TZ)
            season = svc.create_season(program.id, f"{label} Season")
            league = svc.create_league(season.id, f"{label} League")
            svc.create_division(season.id, f"{label} Div", league_id=league.id)
            club = svc.create_club(f"{label} Club")
            # The Team is what gives the Club its Program edge — a Club with no
            # Team is genuinely unlinked, so without this the whole fixture
            # would be axis-free and every refusal below vacuous.
            svc.create_team(club_id=club.id, name=f"{label} Team",
                            program_id=program.id, league_id=league.id)
            org = svc.create_organization(f"{label} Org")
            venue = svc.create_venue(f"{label} Venue", organization_id=org.id,
                                     league_id=program.id)

            official_code = f"{label.upper()}_OFF"
            res = api.commit_officials_availability_import(
                {"officials_csv":
                 "official_code,name,home_club_name\n"
                 f"{official_code},{label} Official,{club.name}\n"},
                actor_id="seed")
            assert res.get("committed"), res
            rink_code = f"{label.upper()}_RINK"
            res = api.commit_rinks_ice_slots_import(
                {"rinks_csv": "venue_name,rink_code,rink_name\n"
                 f"{venue.name},{rink_code},{label} Rink\n"},
                actor_id="seed")
            assert res.get("committed"), res

            official = next(o for o in self.store.all_officials()
                            if o.external_ref == official_code)
            rink = next(r for r in self.store.all_rinks()
                        if r.external_ref == rink_code)
            w[key] = {
                "program": program.id, "season": season.id,
                "league": league.id, "club": club.id, "club_name": club.name,
                "org": org.id, "venue": venue.id, "venue_name": venue.name,
                "official": official.id, "official_code": official_code,
                "rink": rink.id, "rink_code": rink_code,
            }
        self._assert_fixture_is_axis_bearing(w)
        return w

    def _assert_fixture_is_axis_bearing(self, w):
        """THE PREMISE. Every key this module probes must really resolve to a
        row carrying its half's Program edge — otherwise the gate would be
        passing these payloads through as axis-free and every refusal below
        would be measuring something else."""
        api = srv.STATE.api
        for half in ("a", "b"):
            for kind, id_key in (("club", "club"), ("official", "official"),
                                 ("venue", "venue"), ("rink", "rink")):
                record = api._setup_target_record(kind, w[half][id_key])
                self.assertIsNotNone(record, (half, kind))
                edges, saw_link = api._setup_target_edges(kind, record)
                self.assertTrue(saw_link, f"fixture: {half}/{kind} is unlinked")
                self.assertEqual(
                    {e[0] for e in edges}, {w[half]["program"]},
                    f"fixture: {half}/{kind} does not carry exactly its own "
                    "Program edge, so the comparison under test is not "
                    "reachable through it")

    # -- observation -------------------------------------------------------
    def _snapshot(self):
        snap = {name: {r.id for r in getattr(self.store, getter)()}
                for name, getter in _TABLES}
        snap["availability"] = {
            a.id for o in self.store.all_officials()
            for a in self.store.availability_for_official(o.id)}
        snap["audit"] = {(a.action, a.entity_type, a.entity_id)
                         for a in self.store.all_setup_audit()}
        # The REPARENT itself leaves no new row, so the parent pointers are
        # snapshotted as values: a refused import that had already moved an
        # Official's home Club or a Rink's Venue would otherwise pass a
        # membership-only sweep.
        snap["official_homes"] = {(o.id, o.home_club_id)
                                  for o in self.store.all_officials()}
        snap["rink_venues"] = {(r.id, r.venue_id)
                               for r in self.store.all_rinks()}
        return snap

    def _assert_no_residue(self, before, why):
        after = self._snapshot()
        for name in before:
            self.assertEqual(
                before[name], after[name],
                f"{why}: a REFUSED import left residue in `{name}` — "
                f"added {sorted(map(str, after[name] - before[name]))}, "
                f"removed {sorted(map(str, before[name] - after[name]))}")

    @staticmethod
    def _blind(raw, *values):
        """The refusal bytes with the caller's own echoed values masked.
        Byte-for-byte equality of two refusals is the contract; the only
        legitimate difference is a value the caller itself supplied."""
        for index, value in enumerate(values):
            if value:
                raw = raw.replace(value, f"<KEY{index}>")
        return raw

    # -- the import matrix -------------------------------------------------
    #
    # ``(label, path, build, subject_kind, subject_field)``. ``build(half, tag)``
    # returns the request body; ``subject_*`` names the axis-bearing key the
    # refusal is expected to be ABOUT, so the generic not-found's wording can
    # be asserted exactly rather than pattern-matched.
    def _cases(self):
        w = self.w

        def officials_by_club(half, tag):
            # A BRAND-NEW official_code homed at an EXISTING Program-scoped
            # Club: the create path is not axis-free either, because the new
            # row is born inside whatever Program the club name resolved to.
            return {"officials_csv":
                    "official_code,name,email,home_club_name\n"
                    f"NEW_{tag},{tag},{tag}@example.com,{w[half]['club_name']}\n"}

        def officials_by_code(half, tag):
            # An EXISTING official_code — the UPDATE + REPARENT path. The club
            # is the same one it already homes at, so under the positive
            # control this is a no-op reparent rather than a fixture-destroying
            # move (the moves out of a Program live in ReparentMixin).
            return {"officials_csv":
                    "official_code,name,home_club_name\n"
                    f"{w[half]['official_code']},{tag},{w[half]['club_name']}\n"}

        def availability_only(half, tag):
            # THE SHARPEST PATH, and a deliberate product capability: a payload
            # carrying NOTHING but an official_code writes onto an official a
            # PRIOR commit persisted.
            day = 1 + (self._seq % 27)
            return {"official_availability_csv":
                    "official_code,start_time,end_time,status,note\n"
                    f"{w[half]['official_code']},"
                    f"2033-03-{day:02d}T18:00:00+00:00,"
                    f"2033-03-{day:02d}T20:00:00+00:00,available,{tag}\n"}

        def rinks_by_venue(half, tag):
            return {"rinks_csv":
                    "venue_name,rink_code,rink_name\n"
                    f"{w[half]['venue_name']},NEW_{tag},{tag}\n"}

        def rinks_by_code(half, tag):
            return {"rinks_csv":
                    "venue_name,rink_code,rink_name\n"
                    f"{w[half]['venue_name']},{w[half]['rink_code']},{tag}\n"}

        def rinks_and_slots(half, tag):
            day = 1 + (self._seq % 27)
            return {"rinks_csv":
                    "venue_name,rink_code,rink_name\n"
                    f"{w[half]['venue_name']},{w[half]['rink_code']},{tag}\n",
                    "ice_slots_csv":
                    "rink_code,start_time,end_time,slot_type\n"
                    f"{w[half]['rink_code']},"
                    f"2034-04-{day:02d}T18:00:00+00:00,"
                    f"2034-04-{day:02d}T19:30:00+00:00,game\n"}

        return [
            ("officials/home_club_name", OFFICIALS_PATH, officials_by_club,
             "Club", "club_name"),
            ("officials/official_code", OFFICIALS_PATH, officials_by_code,
             "Official", "official_code"),
            ("official_availability/official_code", OFFICIALS_PATH,
             availability_only, "Official", "official_code"),
            ("rinks/venue_name", RINKS_PATH, rinks_by_venue,
             "Venue", "venue_name"),
            ("rinks/rink_code", RINKS_PATH, rinks_by_code,
             "Rink", "rink_code"),
            ("rinks+ice_slots/rink_code", RINKS_PATH, rinks_and_slots,
             "Rink", "rink_code"),
        ]

    # -- reachable stale states -------------------------------------------
    def _make_stale_program(self, c):
        """The operator's saved row names a Program THAT NO LONGER EXISTS,
        reached through ordinary authenticated calls and no direct store
        access. The premise is asserted, not narrated."""
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

    def _assert_saved_row_still_names(self, user_id, program_id):
        saved = self.store.get_active_context(user_id)
        self.assertIsNotNone(saved, "the saved row vanished with its target")
        self.assertEqual(saved.program_id, program_id, "saved Program moved")


# ---------------------------------------------------------------------------
# 1. THE FALSIFIED PATH, closed — and the falsifier that re-opens it.
# ---------------------------------------------------------------------------
class _FalsifiedImportMixin(_ImportHarness):
    """The exact write the falsification measured, plus the NAMED falsifier
    required of this round.

    ``test_a_name_matched_import_cannot_write_into_an_unchosen_program`` is the
    measurement: a signed-in League Admin with NO saved selection sends an
    officials sheet whose ``home_club_name`` is Program A's Club, and a rinks
    sheet whose ``venue_name`` is Program A's Venue. Before the fix both
    answered 200 and wrote.

    ``test_removing_the_name_to_axis_comparison_reopens_the_write`` is the
    FALSIFIER, and it is what makes the assertion above non-vacuous. It patches
    the LIVE ``ApiService._import_axis_targets`` to stop resolving names and
    external refs into records — the single production behaviour this whole
    round adds — and re-sends the IDENTICAL request. Under the falsified build
    the write lands, inside Program A, with the operator still having chosen
    nothing: exactly the measured defect. So the named test above fails the
    moment that comparison is removed, which is the property being claimed.
    """

    def _probe_bodies(self):
        return (("officials", OFFICIALS_PATH,
                 {"officials_csv":
                  "official_code,name,home_club_name\n"
                  f"FALSIFY1,Falsified Official,{self.w['a']['club_name']}\n"}),
                ("rinks", RINKS_PATH,
                 {"rinks_csv": "venue_name,rink_code,rink_name\n"
                  f"{self.w['a']['venue_name']},FALSIFY1,Falsified Rink\n"}))

    def test_a_name_matched_import_cannot_write_into_an_unchosen_program(self):
        for label, path, body in self._probe_bodies():
            with self.subTest(endpoint=label):
                c, user_id = self._operator()
                self.assertIsNone(self.store.get_active_context(user_id))
                before = self._snapshot()
                status, raw, resp = self._req(c, "POST", path, body)
                self.assertEqual(
                    status, 409,
                    f"a `{label}` import wrote into a Program the operator "
                    f"never chose, reached by NAME: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / no selection")

    def test_removing_the_name_to_axis_comparison_reopens_the_write(self):
        api = srv.STATE.api
        original = api._import_axis_targets

        def falsified(import_kind, sheets):
            """THE MUTATION: names and external refs stop resolving into
            records, so no key can carry an axis to compare."""
            return [], []

        api._import_axis_targets = falsified
        self.addCleanup(setattr, api, "_import_axis_targets", original)

        label, path, body = self._probe_bodies()[0]
        c, user_id = self._operator()
        self.assertIsNone(self.store.get_active_context(user_id))
        status, raw, resp = self._req(c, "POST", path, body)
        self.assertEqual(
            status, 200,
            "the falsifier did not reopen the write, so "
            "`test_a_name_matched_import_cannot_write_into_an_unchosen_"
            f"program` is not actually pinning the comparison: {raw}")
        self.assertTrue(resp["committed"], raw)
        official = next(o for o in self.store.all_officials()
                        if o.external_ref == "FALSIFY1")
        edges, _saw = api._setup_target_edges("official", official)
        self.assertEqual(
            {e[0] for e in edges}, {self.w["a"]["program"]},
            "the falsified build did not even land the row inside Program A, "
            "so this falsifier is not exercising the defect it claims to")


# ---------------------------------------------------------------------------
# 2. Missing and STALE context: the same stable 409, over both endpoints.
# ---------------------------------------------------------------------------
class _UnchosenContextMixin(_ImportHarness):
    """No saved selection AND a stale saved selection must answer the SAME
    stable 409 for every axis-bearing key of both endpoints, with zero
    residue. A stale row is the state a resolver fallback quietly papers over,
    which is why it is measured separately rather than assumed equivalent."""

    def test_missing_context_refuses_every_axis_bearing_import(self):
        for label, path, build, _kind, _field in self._cases():
            with self.subTest(case=label):
                c, user_id = self._operator()
                body = build("a", self._next("m"))
                before = self._snapshot()
                status, raw, resp = self._req(c, "POST", path, body)
                self.assertEqual(
                    status, 409,
                    f"`{label}` committed with NO chosen context: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / no selection")
                self.assertIsNone(self.store.get_active_context(user_id))

    def test_a_stale_saved_program_refuses_every_axis_bearing_import(self):
        for label, path, build, _kind, _field in self._cases():
            with self.subTest(case=label):
                c, user_id = self._operator()
                stale = self._make_stale_program(c)
                self._assert_saved_row_still_names(user_id, stale)
                body = build("a", self._next("s"))
                before = self._snapshot()
                status, raw, resp = self._req(c, "POST", path, body)
                self.assertEqual(
                    status, 409,
                    f"`{label}` committed against a saved Program that no "
                    f"longer exists: {raw}")
                self.assertEqual(resp["error"]["code"],
                                 "active_context_required", raw)
                self._assert_no_residue(before, f"{label} / stale Program")

    def test_missing_and_stale_answer_byte_identical_refusals(self):
        """One stable 409 for both unchosen states — and it carries no id at
        all, not even one the caller sent, so there is nothing to correlate."""
        secrets = [self.w[h][k] for h in ("a", "b")
                   for k in ("program", "season", "league", "club",
                             "official", "venue", "rink")]
        for label, path, build, _kind, _field in self._cases():
            with self.subTest(case=label):
                answers = {}
                for state in ("missing", "stale"):
                    c, user_id = self._operator()
                    if state == "stale":
                        stale = self._make_stale_program(c)
                        self._assert_saved_row_still_names(user_id, stale)
                    status, raw, _resp = self._req(
                        c, "POST", path, build("a", self._next(state)))
                    self.assertEqual(status, 409, raw)
                    for secret in secrets:
                        self.assertNotIn(
                            secret, raw,
                            f"`{label}`: the 409 names {secret}, which this "
                            "caller was never told they may see")
                    answers[state] = (status, raw)
                self.assertEqual(
                    len(set(answers.values())), 1,
                    f"`{label}`: the missing-context and stale-context "
                    f"refusals are distinguishable — {answers}")


# ---------------------------------------------------------------------------
# 3. A DIFFERENT saved Program: the generic not-found, and no residue.
# ---------------------------------------------------------------------------
class _ForeignKeyMixin(_ImportHarness):
    """With Program B explicitly and validly saved, every key that resolves
    into Program A answers the facade's own generic
    ``"<Label> <key> not found."`` — the same wording a genuinely absent row
    produces — and writes nothing at all.

    The message may echo only the value the CALLER supplied. Anything else in
    the body (an id, a venue name they never sent, a slot id from the overlap
    gate) would be a disclosure, and the sweep below fails on it.
    """

    def test_a_different_saved_program_answers_a_generic_not_found(self):
        for label, path, build, kind, field in self._cases():
            with self.subTest(case=label):
                c, _user_id = self._operator()
                self._select(c, self.w["b"]["program"],
                             self.w["b"]["season"])
                body = build("a", self._next("f"))
                before = self._snapshot()
                status, raw, resp = self._req(c, "POST", path, body)
                self.assertEqual(
                    status, 404,
                    f"`{label}` named a Program-A key while Program B was "
                    f"saved and was not refused generically: {raw}")
                self.assertEqual(resp["error"]["code"], "not_found", raw)
                self.assertEqual(
                    resp["error"]["message"],
                    f"{kind} {self.w['a'][field]} not found.", raw)
                self._assert_no_residue(before, f"{label} / foreign key")

                supplied = "\n".join(str(v) for v in body.values())
                for secret in (self.w["a"]["program"], self.w["a"]["season"],
                               self.w["a"]["league"], self.w["a"]["club"],
                               self.w["a"]["official"], self.w["a"]["venue"],
                               self.w["a"]["rink"]):
                    if secret in supplied:
                        continue
                    self.assertNotIn(
                        secret, raw,
                        f"`{label}`: the refusal names {secret}, which this "
                        "caller never sent and may not see")

    def test_the_exact_saved_program_succeeds(self):
        """THE POSITIVE CONTROL. Without it every refusal in this module is
        vacuous — a route that refused everything would satisfy them all."""
        for label, path, build, _kind, _field in self._cases():
            with self.subTest(case=label):
                c, _user_id = self._operator()
                self._select(c, self.w["a"]["program"],
                             self.w["a"]["season"])
                status, raw, resp = self._req(
                    c, "POST", path, build("a", self._next("ok")))
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused under its OWN exact saved "
                    f"Program: {raw}")
                self.assertNotIn("error", resp, raw)
                self.assertTrue(resp["committed"], raw)

    def test_a_program_only_selection_is_enough(self):
        """Neither endpoint consumes a Season — facility Season edges are
        ACCESS GRANTS and an Official outlives every Season — so a
        Program-ONLY selection must work. Demanding a Season here would make
        the import unusable on a Program whose last Season was deleted."""
        for label, path, build, _kind, _field in self._cases():
            with self.subTest(case=label):
                c, user_id = self._operator()
                self._select(c, self.w["a"]["program"])       # Program ONLY
                saved = self.store.get_active_context(user_id)
                self.assertEqual((saved.program_id, saved.season_id),
                                 (self.w["a"]["program"], None),
                                 "the fixture did not achieve a Program-ONLY "
                                 "row")
                status, raw, _resp = self._req(
                    c, "POST", path, build("a", self._next("po")))
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused under a Program-ONLY selection, "
                    f"an axis it does not consume: {raw}")


# ---------------------------------------------------------------------------
# 4. The pure REFERENCE key is not an existence oracle.
# ---------------------------------------------------------------------------
class _ReferenceOracleMixin(_ImportHarness):
    """``official_availability[].official_code`` is a pure REFERENCE — the
    sheet exists so an operator can import officials once and their windows
    later — so the code MUST already name a persisted Official.

    THE DEFECT THIS CLOSES. ``validate_official_availability`` computes
    "Unknown official_code {code}" from ``store.all_officials()`` across the
    WHOLE install, and it ran before any context decision existed. So a caller
    with no selection could tell an EXISTING code (the commit proceeded) from a
    nonexistent one (an error report naming it) — an existence oracle over
    every Program's officials, readable off a 200 body.

    WHAT IS ASSERTED. A real Program-A code and a code that names nothing
    answer the SAME BYTES in both states that matter: unselected (one stable
    409 carrying no key at all) and Program-B-selected (one generic 404, equal
    after masking the code the caller itself supplied). Zero residue in both.
    """

    def _availability_body(self, code):
        day = 1 + (self._seq % 27)
        self._seq += 1
        return {"official_availability_csv":
                "official_code,start_time,end_time,status\n"
                f"{code},2035-05-{day:02d}T18:00:00+00:00,"
                f"2035-05-{day:02d}T20:00:00+00:00,available\n"}

    def test_an_unselected_caller_cannot_tell_a_real_code_from_a_ghost(self):
        real = self.w["a"]["official_code"]
        ghost = f"GHOST_{uuid.uuid4().hex[:8]}"
        answers = {}
        for state, code in (("real Program-A code", real), ("ghost", ghost)):
            c, user_id = self._operator()
            self.assertIsNone(self.store.get_active_context(user_id))
            before = self._snapshot()
            status, raw, resp = self._req(
                c, "POST", OFFICIALS_PATH, self._availability_body(code))
            self.assertEqual(
                status, 409,
                f"an availability-only import naming a {state} answered "
                f"{status} instead of the stable axis refusal: {raw}")
            self.assertEqual(resp["error"]["code"], "active_context_required",
                             raw)
            self.assertNotIn(code, raw,
                             "the 409 echoes the probed code, which makes it "
                             "correlatable")
            self._assert_no_residue(before, f"availability / unselected / {state}")
            answers[state] = (status, raw)
        self.assertEqual(
            len(set(answers.values())), 1,
            f"an unselected caller can tell an existing official_code from a "
            f"nonexistent one — {answers}")

    def test_a_selected_caller_cannot_tell_a_foreign_code_from_a_ghost(self):
        real = self.w["a"]["official_code"]
        ghost = f"GHOST_{uuid.uuid4().hex[:8]}"
        answers = {}
        for state, code in (("real Program-A code", real), ("ghost", ghost)):
            c, _user_id = self._operator()
            self._select(c, self.w["b"]["program"], self.w["b"]["season"])
            before = self._snapshot()
            status, raw, resp = self._req(
                c, "POST", OFFICIALS_PATH, self._availability_body(code))
            self.assertEqual(
                status, 404,
                f"an availability-only import naming a {state} answered "
                f"{status} instead of the generic not-found: {raw}")
            self.assertEqual(resp["error"]["code"], "not_found", raw)
            self.assertEqual(resp["error"]["message"],
                             f"Official {code} not found.", raw)
            self._assert_no_residue(before, f"availability / selected / {state}")
            answers[state] = (status, self._blind(raw, code))
        self.assertEqual(
            len(set(answers.values())), 1,
            f"a foreign official_code and a nonexistent one answer "
            f"differently under a VALID selection — {answers}")

    def test_the_policy_advisory_and_slot_gates_sit_behind_the_refusal(self):
        """The rinks commit's pre-write gates name existing slot ids and leak
        Program-scope curfew/buffer policy. All of it must be unreachable for
        an unselected caller: the same stable 409, no slot id, no warning."""
        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        status, raw, _ = self._req(
            c, "POST", RINKS_PATH,
            {"rinks_csv": "venue_name,rink_code,rink_name\n"
             f"{self.w['a']['venue_name']},{self.w['a']['rink_code']},Seed\n",
             "ice_slots_csv": "rink_code,start_time,end_time,slot_type\n"
             f"{self.w['a']['rink_code']},2036-06-01T22:00:00+00:00,"
             "2036-06-01T23:00:00+00:00,game\n"})
        self.assertEqual(status, 200, raw)
        seeded = [s for s in self.store.all_ice_slots()
                  if s.rink_id == self.w["a"]["rink"]]
        self.assertTrue(seeded, "the fixture slot was not persisted, so the "
                                "overlap gate below cannot fire")

        # A NON-exact overlap on that same Program-A rink: normally a
        # ScheduleConflictError whose details carry rink_id and
        # conflict_slot_id.
        overlapping = {
            "rinks_csv": "venue_name,rink_code,rink_name\n"
            f"{self.w['a']['venue_name']},{self.w['a']['rink_code']},Probe\n",
            "ice_slots_csv": "rink_code,start_time,end_time,slot_type\n"
            f"{self.w['a']['rink_code']},2036-06-01T22:30:00+00:00,"
            "2036-06-01T23:30:00+00:00,game\n"}
        c2, _uid = self._operator()
        before = self._snapshot()
        status, raw, resp = self._req(c2, "POST", RINKS_PATH, overlapping)
        self.assertEqual(
            status, 409,
            f"an unselected caller reached the overlap gate: {raw}")
        self.assertEqual(resp["error"]["code"], "active_context_required", raw)
        for slot in seeded:
            self.assertNotIn(
                slot.id, raw,
                "the refusal discloses an ice slot id from a Program this "
                "caller has not selected")
        self.assertNotIn(self.w["a"]["rink"], raw)
        self._assert_no_residue(before, "rinks overlap probe / unselected")


# ---------------------------------------------------------------------------
# 5. FAIL CLOSED: keys that span Programs or do not determine one axis.
# ---------------------------------------------------------------------------
class _FailClosedMixin(_ImportHarness):
    """Four distinct ways one payload can name more than one Program — or
    none that can be judged — and all four take the SAME stable 409 rather
    than a guess. Program A is validly saved throughout, so a 409 here can
    only mean the payload itself failed to determine one axis."""

    def _assert_fail_closed(self, c, label, path, body):
        before = self._snapshot()
        status, raw, resp = self._req(c, "POST", path, body)
        self.assertEqual(
            status, 409,
            f"`{label}` did not fail closed — the gate picked a Program for a "
            f"payload that names more than one: {raw}")
        self.assertEqual(resp["error"]["code"], "active_context_required", raw)
        self._assert_no_residue(before, f"{label} / fail closed")
        return raw

    def test_rows_spanning_two_programs_fail_closed(self):
        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        self._assert_fail_closed(
            c, "officials: row A club + row B club", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"SPAN_A,Span A,{self.w['a']['club_name']}\n"
             f"SPAN_B,Span B,{self.w['b']['club_name']}\n"})
        self._assert_fail_closed(
            c, "rinks: row A venue + row B venue", RINKS_PATH,
            {"rinks_csv": "venue_name,rink_code,rink_name\n"
             f"{self.w['a']['venue_name']},SPAN_A,Span A\n"
             f"{self.w['b']['venue_name']},SPAN_B,Span B\n"})

    def test_a_reparent_across_programs_fails_closed(self):
        """SOURCE vs DESTINATION in ONE row: the matched row's CURRENT axis and
        the axis it is being moved into. Both are named by the same row, and a
        payload that names two must not resolve to either."""
        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        self._assert_fail_closed(
            c, "rink A -> venue B", RINKS_PATH,
            {"rinks_csv": "venue_name,rink_code,rink_name\n"
             f"{self.w['b']['venue_name']},{self.w['a']['rink_code']},Moved\n"})
        self._assert_fail_closed(
            c, "official A -> club B", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"{self.w['a']['official_code']},Moved,{self.w['b']['club_name']}\n"})

    def test_one_key_carrying_two_programs_fails_closed(self):
        """ONE KEY, MANY PROGRAMS — structurally distinct from row-vs-row.

        A Club genuinely "owns Teams across Programs and Leagues", so a SINGLE
        ``home_club_name`` can resolve to a SINGLE row whose own edge set spans
        two Programs. Row-vs-row unions one Program from each of two keys;
        this unions two out of ONE key, i.e. the multi-edge return of
        ``_setup_target_edges`` rather than the accumulation across keys. It
        also pins the half a naive `program.id in row_programs` check would get
        wrong: Program A IS among the Club's Programs and is validly saved, so
        a containment test would ADMIT this write, and only comparing the
        payload's whole resolved axis set against one refuses it.
        """
        api = srv.STATE.api
        club = api.setup.create_club(self._next("Spanning Club"))
        for half in ("a", "b"):
            api.setup.create_team(club_id=club.id,
                                  name=self._next(f"Spanning Team {half}"),
                                  program_id=self.w[half]["program"],
                                  league_id=self.w[half]["league"])
        edges, saw_link = api._setup_target_edges(
            "club", self.store.get_club(club.id))
        self.assertTrue(saw_link)
        self.assertEqual(
            {e[0] for e in edges},
            {self.w["a"]["program"], self.w["b"]["program"]},
            "the fixture club does not actually carry two Programs on ONE "
            "key, so this test would collapse into the row-vs-row case")

        c, _user_id = self._operator()
        # Program A is VALIDLY saved and IS one of the Club's Programs.
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        self._assert_fail_closed(
            c, "officials: one club, two Programs", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"MANY1,Many,{club.name}\n"})

    def test_a_key_linked_but_unjudgeable_fails_closed(self):
        """``saw_link`` True with an EMPTY edge set — the fail-closed signal the
        mutation family already honours. Built through a Team whose
        ``program_id`` says Program A while its ``league_id`` resolves to a real
        League in Program B: ``_team_edges`` reports that as LINKED BUT
        UNAUTHORIZED, and the Club that owns it inherits the verdict."""
        api = srv.STATE.api
        club = api.setup.create_club(self._next("Unjudgeable Club"))
        team = api.setup.create_team(club_id=club.id,
                                     name=self._next("Unjudgeable Team"),
                                     program_id=self.w["a"]["program"],
                                     league_id=self.w["a"]["league"])
        team = self.store.get_team(team.id)
        team.league_id = self.w["b"]["league"]     # a REAL League in Program B
        self.store.save_team(team)
        edges, saw_link = api._setup_target_edges(
            "club", self.store.get_club(club.id))
        self.assertEqual((edges, saw_link), (set(), True),
                         "the fixture is not actually linked-but-unjudgeable, "
                         "so this test proves nothing")

        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        self._assert_fail_closed(
            c, "officials: unjudgeable club", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"UNJ1,Unjudgeable,{club.name}\n"})

    def test_the_fail_closed_refusals_are_the_same_bytes_as_no_context(self):
        """All of them, and the missing-context case, are ONE answer."""
        c_missing, _uid = self._operator()
        _s, missing_raw, _r = self._req(
            c_missing, "POST", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"SAME1,Same,{self.w['a']['club_name']}\n"})
        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        span_raw = self._assert_fail_closed(
            c, "officials span", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"SAME2,Same,{self.w['a']['club_name']}\n"
             f"SAME3,Same,{self.w['b']['club_name']}\n"})
        self.assertEqual(
            missing_raw, span_raw,
            "the missing-context 409 and the spanning-keys 409 are "
            "distinguishable, so the status alone reports which condition "
            "fired")


# ---------------------------------------------------------------------------
# 6. The REPARENT's source axis is what protects a row from leaving.
# ---------------------------------------------------------------------------
class _ReparentMixin(_ImportHarness):
    """A move OUT of Program A into a brand-new, unlinked destination must not
    be admitted merely because the destination has no axis to compare. The
    SOURCE axis — the matched Rink's current Venue, the matched Official's
    current home Club — is carried into the comparison for exactly this."""

    def test_a_foreign_row_cannot_be_moved_onto_an_axis_free_destination(self):
        c, _user_id = self._operator()
        self._select(c, self.w["b"]["program"], self.w["b"]["season"])

        fresh_venue = self._next("Unlinked Venue")
        before = self._snapshot()
        status, raw, resp = self._req(
            c, "POST", RINKS_PATH,
            {"rinks_csv": "venue_name,rink_code,rink_name\n"
             f"{fresh_venue},{self.w['a']['rink_code']},Moved\n"})
        self.assertEqual(
            status, 404,
            "a Program-A rink was moved onto a brand-new unlinked venue by an "
            f"operator standing in Program B — it silently EXITS A: {raw}")
        self.assertEqual(resp["error"]["message"],
                         f"Rink {self.w['a']['rink_code']} not found.", raw)
        self._assert_no_residue(before, "rink reparent out of A")
        self.assertEqual(
            self.store.get_rink(self.w["a"]["rink"]).venue_id,
            self.w["a"]["venue"], "the rink's Venue moved despite the refusal")

        # ...and the Official equivalent: a blank home_club_name DETACHES,
        # which destroys the Program axis just as thoroughly as a move.
        before = self._snapshot()
        status, raw, resp = self._req(
            c, "POST", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"{self.w['a']['official_code']},Detached,NA\n"})
        self.assertEqual(
            status, 404,
            "a Program-A official was detached from its club by an operator "
            f"standing in Program B: {raw}")
        self.assertEqual(resp["error"]["message"],
                         f"Official {self.w['a']['official_code']} not found.",
                         raw)
        self._assert_no_residue(before, "official detach out of A")
        self.assertEqual(
            self.store.get_official(self.w["a"]["official"]).home_club_id,
            self.w["a"]["club"],
            "the official's home Club moved despite the refusal")

    def test_the_owning_operator_may_still_move_their_own_row(self):
        """The other half, without which the rule above would be satisfied by
        refusing every reparent. Standing in Program A, the operator may move
        their OWN rink to a new venue of their own — the destination carries no
        axis, and nothing crosses a Program boundary."""
        c, _user_id = self._operator()
        self._select(c, self.w["a"]["program"], self.w["a"]["season"])
        fresh_venue = self._next("Own Venue")
        status, raw, resp = self._req(
            c, "POST", RINKS_PATH,
            {"rinks_csv": "venue_name,rink_code,rink_name\n"
             f"{fresh_venue},{self.w['a']['rink_code']},Moved\n"})
        self.assertEqual(status, 200, raw)
        self.assertTrue(resp["committed"], raw)
        moved = self.store.get_rink(self.w["a"]["rink"])
        self.assertNotEqual(moved.venue_id, self.w["a"]["venue"], raw)


# ---------------------------------------------------------------------------
# 7. The retained AXIS-FREE path, and its measurement.
# ---------------------------------------------------------------------------
class _AxisFreeMixin(_ImportHarness):
    """The pilot's FIRST import — nothing in the payload resolves to anything —
    is genuinely axis-free and must keep working with no selection at all.

    The proof is not a 200. It is a spy on ``ContextService._fallback``
    recording ZERO calls, plus a spy on ``ApiService._explicit_selection``
    recording ZERO calls, plus a direct assertion that
    ``_import_axis_targets`` resolved NOTHING for that payload. The
    ``_fallback`` spy is the one that matters for "no Program nobody chose was
    brought into the decision": ``_resolve_locked`` honours a valid saved row
    without ever reaching it, so a zero-call assertion on the resolver alone
    would pass for the wrong reason.
    """

    def _spies(self):
        api = srv.STATE.api
        fallback_calls, selection_calls = [], []

        original_fallback = ctx_mod.ContextService._fallback

        def fallback_spy(inner_self, *args, **kwargs):
            fallback_calls.append(args[:1])
            return original_fallback(inner_self, *args, **kwargs)

        ctx_mod.ContextService._fallback = fallback_spy
        self.addCleanup(setattr, ctx_mod.ContextService, "_fallback",
                        original_fallback)

        original_selection = api._explicit_selection

        def selection_spy(*args, **kwargs):
            selection_calls.append(args[:1])
            return original_selection(*args, **kwargs)

        api._explicit_selection = selection_spy
        self.addCleanup(setattr, api, "_explicit_selection",
                        original_selection)
        return fallback_calls, selection_calls

    def test_a_first_import_resolves_no_axis_and_reads_no_context(self):
        tag = uuid.uuid4().hex[:8]
        payloads = (
            ("officials", OFFICIALS_PATH,
             {"officials_csv": "official_code,name,home_club_name\n"
              f"FRESH_{tag},Fresh Official,Fresh Club {tag}\n"},
             "officials_availability",
             {"officials": [{"official_code": f"FRESH_{tag}",
                             "name": "Fresh Official",
                             "home_club_name": f"Fresh Club {tag}"}]}),
            ("rinks", RINKS_PATH,
             {"rinks_csv": "venue_name,rink_code,rink_name\n"
              f"Fresh Venue {tag},FRESH_{tag},Fresh Rink\n"},
             "rinks_ice_slots",
             {"rinks": [{"venue_name": f"Fresh Venue {tag}",
                         "rink_code": f"FRESH_{tag}",
                         "rink_name": "Fresh Rink"}]}),
        )
        api = srv.STATE.api
        for label, path, body, import_kind, sheets in payloads:
            with self.subTest(endpoint=label):
                self.assertEqual(
                    api._import_axis_targets(import_kind, sheets), ([], []),
                    f"the `{label}` payload this case calls axis-free DOES "
                    "resolve a persisted row, so a 200 below would prove "
                    "nothing")
                c, user_id = self._operator()
                self.assertIsNone(self.store.get_active_context(user_id))
                fallback_calls, selection_calls = self._spies()
                status, raw, resp = self._req(c, "POST", path, body)
                self.assertEqual(
                    status, 200,
                    f"a genuinely axis-free first `{label}` import was "
                    f"refused, which makes the pilot bootstrap impossible: "
                    f"{raw}")
                self.assertTrue(resp["committed"], raw)
                self.assertEqual(
                    selection_calls, [],
                    f"the axis-free `{label}` import read the ActiveContext "
                    f"{len(selection_calls)} time(s); nothing resolved, so "
                    "there was nothing for a selection to be compared against")
                self.assertEqual(
                    fallback_calls, [],
                    f"the axis-free `{label}` import consulted "
                    f"ContextService._fallback {len(fallback_calls)} time(s) "
                    "— a Program nobody chose being brought into the decision")
                self.assertIsNone(
                    self.store.get_active_context(user_id),
                    "the import persisted a selection as a side effect")

    def test_an_unlinked_match_is_admitted_without_a_fallback(self):
        """A key that DOES resolve, to a row carrying no Program edge, has
        nothing to compare — the same clause ``_create_parent_is_axis_free``
        applies. It reads the saved row (that read is what proves nothing needs
        comparing) but must never reach the resolver's fallback."""
        api = srv.STATE.api
        tag = uuid.uuid4().hex[:8]
        # A Club with no Team at all is genuinely unlinked, by construction.
        club = api.setup.create_club(f"Unlinked Club {tag}")
        edges, saw_link = api._setup_target_edges(
            "club", self.store.get_club(club.id))
        self.assertEqual((edges, saw_link), (set(), False),
                         "the fixture club is not genuinely unlinked")

        c, user_id = self._operator()
        self.assertIsNone(self.store.get_active_context(user_id))
        fallback_calls, _selection_calls = self._spies()
        status, raw, resp = self._req(
            c, "POST", OFFICIALS_PATH,
            {"officials_csv": "official_code,name,home_club_name\n"
             f"UNL_{tag},Unlinked Official,{club.name}\n"})
        self.assertEqual(
            status, 200,
            "an import naming an UNLINKED club was refused; on a "
            "Program-less install that would be unsatisfiable rather than "
            f"strict: {raw}")
        self.assertTrue(resp["committed"], raw)
        self.assertEqual(
            fallback_calls, [],
            f"the unlinked-match import consulted ContextService._fallback "
            f"{len(fallback_calls)} time(s)")


# ---------------------------------------------------------------------------
# Concrete backends.
# ---------------------------------------------------------------------------
def _backends(name, mixin):
    memory = type(f"{name}MemoryTest", (mixin, unittest.TestCase),
                  {"DATABASE_URL": None})

    def sqlite_setup(cls):
        cls.DATABASE_URL = _sqlite_url()

    sqlite = type(f"{name}SqliteTest", (mixin, unittest.TestCase),
                  {"setUpClass": classmethod(sqlite_setup)})

    def postgres_setup(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)

    postgres = type(f"{name}PostgresTest", (mixin, unittest.TestCase),
                    {"setUpClass": classmethod(postgres_setup)})
    return memory, sqlite, postgres


(FalsifiedImportMemoryTest, FalsifiedImportSqliteTest,
 FalsifiedImportPostgresTest) = _backends("FalsifiedImport",
                                          _FalsifiedImportMixin)
(UnchosenContextMemoryTest, UnchosenContextSqliteTest,
 UnchosenContextPostgresTest) = _backends("UnchosenContext",
                                          _UnchosenContextMixin)
(ForeignKeyMemoryTest, ForeignKeySqliteTest,
 ForeignKeyPostgresTest) = _backends("ForeignKey", _ForeignKeyMixin)
(ReferenceOracleMemoryTest, ReferenceOracleSqliteTest,
 ReferenceOraclePostgresTest) = _backends("ReferenceOracle",
                                          _ReferenceOracleMixin)
(FailClosedMemoryTest, FailClosedSqliteTest,
 FailClosedPostgresTest) = _backends("FailClosed", _FailClosedMixin)
(ReparentMemoryTest, ReparentSqliteTest,
 ReparentPostgresTest) = _backends("Reparent", _ReparentMixin)
(AxisFreeMemoryTest, AxisFreeSqliteTest,
 AxisFreePostgresTest) = _backends("AxisFree", _AxisFreeMixin)


# ---------------------------------------------------------------------------
# 8. The two-connection context-switch/import race — PostgreSQL only.
# ---------------------------------------------------------------------------
class PostgresImportSwitchRaceTest(unittest.TestCase):
    """Only the CONTEXT-CONSISTENT outcome may commit.

    THE HAZARD. ``_import_context_error`` reads the operator's saved
    ActiveContext row and then the commit writes. If a concurrent
    ``POST /api/context`` could commit a DIFFERENT tuple in between, the import
    would land authorized by a tuple that was never in effect when the decision
    was taken — the same check/use gap #386 closed for the scheduler, #410 for
    the ice commit and #409 for the setup creates.

    THE CLAIM. ``setup_guarded_import`` takes the ActiveContext ROW LOCK as the
    FIRST statement of the transaction that performs the whole commit
    (``_explicit_selection(lock=True)`` -> ``get_active_context_for_update`` ->
    ``pg_advisory_xact_lock`` on the per-user key), which is the same key
    ``set_active_context`` takes. So a switch either orders wholly BEFORE the
    decision or waits for the write it would have authorized.

    WHY TWO CONNECTIONS. ``SqlStore`` holds exactly one connection and its
    ``transaction()`` holds a per-instance ``RLock`` for the whole block, so two
    concurrent requests to one server never hold two open PostgreSQL
    transactions. The switcher therefore runs on a SEPARATE ``SqlStore``, which
    is the only shape in which the advisory lock can be the mechanism that
    orders them.

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
        api = srv.STATE.api
        svc = api.setup

        self.ids = {}
        for key, label in (("a", "RaceA"), ("b", "RaceB")):
            program = svc.create_program(f"{label} Program", timezone_name=TZ)
            season = svc.create_season(program.id, f"{label} Season")
            league = svc.create_league(season.id, f"{label} League")
            club = svc.create_club(f"{label} Club")
            svc.create_team(club_id=club.id, name=f"{label} Team",
                            program_id=program.id, league_id=league.id)
            org = svc.create_organization(f"{label} Org")
            venue = svc.create_venue(f"{label} Venue", organization_id=org.id,
                                     league_id=program.id)
            res = api.commit_rinks_ice_slots_import(
                {"rinks_csv": "venue_name,rink_code,rink_name\n"
                 f"{venue.name},{label.upper()}_RINK,{label} Rink\n"},
                actor_id="seed")
            assert res.get("committed"), res
            self.ids[key] = {"program": program.id, "season": season.id,
                             "club": club.id, "club_name": club.name,
                             "venue_name": venue.name,
                             "rink_code": f"{label.upper()}_RINK"}

        account = api.accounts.create_account(
            f"imprace_{uuid.uuid4().hex[:8]}", "demo", Role.LEAGUE_ADMIN)
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

    def _pause(self, seconds):
        """Block A after the LOCKING context read of its own import, i.e.
        'A holds the per-user ActiveContext lock and has not committed'. No
        production file is touched — the LIVE bound method is wrapped."""
        api = srv.STATE.api
        original = api._explicit_selection
        reached = threading.Event()

        def paused(user_id, role, scope, lock=False):
            result = original(user_id, role, scope, lock=lock)
            if lock and user_id == self.user_id:
                reached.set()
                time.sleep(seconds)
            return result

        api._explicit_selection = paused
        self.addCleanup(setattr, api, "_explicit_selection", original)
        return reached

    def _race(self, connection_a, connection_b):
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
        thread_a.join(60)
        thread_b.join(60)
        self.assertFalse(thread_a.is_alive(), "connection A never returned")
        self.assertFalse(thread_b.is_alive(), "connection B never returned")
        for name, exc in errors.items():
            raise AssertionError(f"connection {name} failed: {exc!r}")

    def test_a_switch_cannot_land_inside_the_guarded_import(self):
        """A has Program A saved and imports onto Program A's rink. B switches
        A's saved context to Program B while A is inside the guarded import.
        The only admissible outcomes are context-consistent: A commits under
        the Program it had CHOSEN, or A is refused — never a commit authorized
        by a tuple that was never in effect."""
        c = self._login()
        status, raw, _ = self._req(c, "POST", "/api/context",
                                   {"program_id": self.ids["a"]["program"],
                                    "season_id": self.ids["a"]["season"]})
        self.assertEqual(status, 200, raw)

        reached = self._pause(1.5)
        result = {}

        def connection_a():
            result["response"] = self._req(
                c, "POST", RINKS_PATH,
                {"rinks_csv": "venue_name,rink_code,rink_name\n"
                 f"{self.ids['a']['venue_name']},"
                 f"{self.ids['a']['rink_code']},Raced\n",
                 "ice_slots_csv": "rink_code,start_time,end_time,slot_type\n"
                 f"{self.ids['a']['rink_code']},"
                 "2037-07-07T18:00:00+00:00,"
                 "2037-07-07T19:30:00+00:00,game\n"})

        def connection_b():
            self.assertTrue(reached.wait(20), "A never reached the pause seam")
            self.store_b.set_active_context(ActiveContext(
                id=self.user_id, program_id=self.ids["b"]["program"],
                season_id=self.ids["b"]["season"],
                updated_at=datetime.now(timezone.utc)))

        self._race(connection_a, connection_b)

        status, raw, resp = result["response"]
        self.assertIn(status, (200, 404, 409), raw)
        rink = next(r for r in self.store.all_rinks()
                    if r.external_ref == self.ids["a"]["rink_code"])
        slots = [s for s in self.store.all_ice_slots() if s.rink_id == rink.id]
        if status == 200:
            self.assertTrue(resp["committed"], raw)
            self.assertEqual(
                len(slots), 1,
                "the import committed but its ice slot is missing, so the "
                "write was not atomic with the decision")
            edges, _saw = srv.STATE.api._setup_target_edges("rink", rink)
            self.assertEqual(
                {e[0] for e in edges}, {self.ids["a"]["program"]},
                "the committed import landed on ice outside the Program that "
                f"was saved when the decision was taken: {raw}")
        else:
            self.assertEqual(
                slots, [],
                "the import was refused but left an ice slot behind: "
                f"{raw}")
        # Whatever happened, B's switch is the one that persisted.
        saved = self.store.get_active_context(self.user_id)
        self.assertEqual(saved.program_id, self.ids["b"]["program"])
