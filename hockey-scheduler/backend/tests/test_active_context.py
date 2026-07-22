"""Per-user active Program/Season context — backend selection foundation (#159).

Which Program + Season a user is currently working in, persisted per user. This
is a backend PREFERENCE + RESOLUTION foundation only — NOT the shell switcher/UI,
NOT deep-link restoration, NOT cross-context isolation of existing reads/writes/
workers/exports (those still take explicit ids), and NOT completion of #159.

The selection is a VIEW preference, never authority: on EVERY resolve and set it
is filtered through the caller's real role + account scope, so a scoped account
can neither select nor enumerate a Program/Season outside its scope. Program-only
selection (no Season) is supported; an archived Season is honored as a read-only
historical context, never silently replaced.

Coverage: Memory/SQLite/PostgreSQL parity for the authorization matrix,
non-oracle errors, program-only + archived-read-only, deterministic semantic-date
fallback, stale-scope/deleted-record behavior, idempotent/last-write set; a
durable migration-044 upgrade+reopen proof; and a strict authenticated HTTP
contract.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import ActiveContext, Role, SeasonStatus
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.sql_store import migrate
from hockey_scheduler.web.server import STATE, Handler

_VERSION = "044_active_context"
_ADMIN = (Role.LEAGUE_ADMIN, {})
_EMPTY = {"program_id": None, "season_id": None, "read_only": False,
          "program": None, "season": None}


def _sql_backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _seed(api):
    """Two Programs, each with an active Season and a registered Team, so a
    Coach scoped to one Team is authorized for exactly one Program+Season."""
    club = api.create_club("Club")["id"]

    def program(pn, sn, tn):
        pid = api.create_program(pn, "US", "UTC")["id"]
        sid = api.create_season(pid, sn)["id"]
        lid = api.create_league(sid, pn + " League")["id"]
        did = api.create_division(sid, "D", league_id=lid)["id"]
        tid = api.create_team(club_id=club, name=tn, league_id=lid,
                              division_id=did)["id"]
        api.setup.register_team_for_season(sid, tid, did)
        return pid, sid, tid

    p1, s1, t1 = program("P1", "S1", "Alpha")
    p2, s2, t2 = program("P2", "S2", "Beta")
    return dict(p1=p1, s1=s1, t1=t1, p2=p2, s2=s2, t2=t2)


def _set_start(store, season_id, dt):
    s = store.get_season(season_id)
    s.start_date = dt
    store.save_season(s)


def _archive(store, season_id):
    s = store.get_season(season_id)
    s.status = SeasonStatus.ARCHIVED
    store.save_season(s)


class ContextAuthorizationTest(unittest.TestCase):
    """Resolve/set filter through the caller's real authorized scope on every
    request — a scoped account cannot select or enumerate an unrelated context,
    and a stale saved selection is dropped the instant scope changes."""

    def test_operator_sees_all_scoped_coach_sees_only_own(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ids = _seed(api)
                coach = (Role.COACH, {"team_id": ids["t1"]})
                # League Admin may select either Program/Season.
                self.assertNotIn(
                    "error", api.set_active_context("adm", *_ADMIN,
                                                    ids["p2"], ids["s2"]))
                # Coach resolves to its own team's context...
                c = api.get_active_context("cu", *coach)
                self.assertEqual((c["program_id"], c["season_id"]),
                                 (ids["p1"], ids["s1"]), label)
                # ...may set it...
                self.assertNotIn("error", api.set_active_context(
                    "cu", *coach, ids["p1"], ids["s1"]))
                # ...but not another Program (exists, not theirs) NOR a ghost —
                # BOTH the SAME non-oracle not_found (no existence oracle).
                other = api.set_active_context("cu", *coach, ids["p2"], ids["s2"])
                ghost = api.set_active_context("cu", *coach, "ghost_prog", None)
                self.assertEqual(other["error"]["code"], "not_found", label)
                self.assertEqual(ghost["error"]["code"], "not_found", label)
                self.assertEqual(other["error"]["message"],
                                 ghost["error"]["message"], label)
                # A Season outside the Program is likewise a generic not_found.
                cross = api.set_active_context("cu", *coach, ids["p1"], ids["s2"])
                self.assertEqual(cross["error"]["details"]["reason"],
                                 "season_not_accessible", label)
                _close(store)

    def test_stale_scope_rebind_and_revoke_refilters_immediately(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ids = _seed(api)
                # Coach on team1 saves its context.
                api.set_active_context("cu", Role.COACH, {"team_id": ids["t1"]},
                                       ids["p1"], ids["s1"])
                # REBOUND to team2 → the saved p1/s1 is no longer authorized, so
                # resolve returns team2's context, never the stale one.
                c = api.get_active_context("cu", Role.COACH,
                                           {"team_id": ids["t2"]})
                self.assertEqual((c["program_id"], c["season_id"]),
                                 (ids["p2"], ids["s2"]), label)
                # REVOKED (teamless) → empty context, never the stale p1/s1.
                c = api.get_active_context("cu", Role.COACH, {"team_id": None})
                self.assertEqual(c, _EMPTY, label)
                _close(store)

    def test_viewer_is_global_readonly(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ids = _seed(api)
                # Under the current #211 model a Viewer is global read-only.
                c = api.get_active_context("vu", Role.VIEWER, {})
                self.assertIn(c["program_id"], (ids["p1"], ids["p2"]), label)
                self.assertNotIn("error", api.set_active_context(
                    "vu", Role.VIEWER, {}, ids["p2"], ids["s2"]))
                _close(store)


class ContextResolveFallbackTest(unittest.TestCase):
    def test_empty_world_is_empty_context(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self.assertEqual(api.get_active_context("u", *_ADMIN), _EMPTY,
                                 label)
                _close(store)

    def test_fallback_prefers_latest_active_season_by_date(self):
        # Deterministic fallback parses the boundary date semantically — the
        # latest start_date wins regardless of insertion/id order; a null-date
        # Season is never preferred over a dated one.
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p = api.create_program("P", "US", "UTC")["id"]
                early = api.create_season(p, "early")["id"]
                late = api.create_season(p, "late")["id"]   # higher id / later insert
                nul = api.create_season(p, "nodate")["id"]
                _set_start(store, early, datetime(2027, 1, 1, tzinfo=timezone.utc))
                _set_start(store, late, datetime(2027, 6, 1, tzinfo=timezone.utc))
                _set_start(store, nul, None)
                c = api.get_active_context("u", *_ADMIN)
                self.assertEqual(c["season_id"], late, label)   # by DATE, not id
                _close(store)

    def test_program_only_fallback_when_no_active_season(self):
        # A Program can exist before its first Season (or with only archived
        # ones): the fallback still selects the Program, with a null Season.
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p = api.create_program("Solo", "US", "UTC")["id"]
                c = api.get_active_context("u", *_ADMIN)
                self.assertEqual(c["program_id"], p, label)
                self.assertIsNone(c["season_id"], label)
                self.assertFalse(c["read_only"], label)
                self.assertIsNotNone(c["program"], label)
                _close(store)

    def test_saved_archived_season_is_kept_read_only(self):
        # An explicit archived-history selection is honored as read-only, NOT
        # silently replaced by an unrelated active Season.
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p = api.create_program("P", "US", "UTC")["id"]
                arch = api.create_season(p, "Old")["id"]
                api.create_season(p, "New")["id"]            # an active alternative
                api.set_active_context("u", *_ADMIN, p, arch)
                _archive(store, arch)
                c = api.get_active_context("u", *_ADMIN)
                self.assertEqual(c["season_id"], arch, label)   # kept, not swapped
                self.assertTrue(c["read_only"], label)          # historical
                _close(store)

    def test_saved_deleted_season_falls_back(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p = api.create_program("P", "US", "UTC")["id"]
                s = api.create_season(p, "S")["id"]
                api.set_active_context("u", *_ADMIN, p, s)
                store.delete_season(s)                       # vanishes
                c = api.get_active_context("u", *_ADMIN)
                self.assertIsNone(c["season_id"], label)     # program-only fallback
                self.assertEqual(c["program_id"], p, label)
                _close(store)

    def test_per_user_isolation(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ids = _seed(api)
                api.set_active_context("u1", *_ADMIN, ids["p2"], ids["s2"])
                self.assertEqual(api.get_active_context("u1", *_ADMIN)["season_id"],
                                 ids["s2"], label)
                # u2 never sees u1's selection — its own deterministic fallback.
                self.assertEqual(api.get_active_context("u2", *_ADMIN)["program_id"],
                                 ids["p1"], label)
                _close(store)


class ContextSetTest(unittest.TestCase):
    def test_program_only_and_archived_read_only_set(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p = api.create_program("P", "US", "UTC")["id"]
                s = api.create_season(p, "S")["id"]
                # Program-only set (null Season) is allowed.
                c = api.set_active_context("u", *_ADMIN, p, None)
                self.assertEqual((c["program_id"], c["season_id"]), (p, None),
                                 label)
                # Selecting an archived Season is accepted as read-only history.
                _archive(store, s)
                c = api.set_active_context("u", *_ADMIN, p, s)
                self.assertEqual(c["season_id"], s, label)
                self.assertTrue(c["read_only"], label)
                _close(store)

    def test_missing_program_id_is_validation_error(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                r = api.set_active_context("u", *_ADMIN, None, None)
                self.assertEqual(r["error"]["code"], "validation_error", label)
                _close(store)

    def test_same_selection_is_idempotent_and_last_write_wins(self):
        for label, store in _sql_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ids = _seed(api)
                a = api.set_active_context("u", *_ADMIN, ids["p1"], ids["s1"])
                b = api.set_active_context("u", *_ADMIN, ids["p1"], ids["s1"])
                self.assertEqual(a, b, label)                # idempotent
                self.assertEqual(len([c for c in [
                    store.get_active_context("u")] if c]), 1, label)  # one row
                # A different selection overwrites (last write wins).
                api.set_active_context("u", *_ADMIN, ids["p2"], ids["s2"])
                self.assertEqual(store.get_active_context("u").season_id,
                                 ids["s2"], label)
                _close(store)


class ActiveContextMigrationTest(unittest.TestCase):
    """Migration 044 is a plain additive table. Prove it applies to an ADOPTED
    (pre-044) database and that a stored selection survives a real close/reopen,
    on file-backed SQLite and PostgreSQL — not only in-memory parity."""

    def _locations(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._tmp = path
        yield "sqlite", path
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", url

    def tearDown(self):
        if getattr(self, "_tmp", None) and os.path.exists(self._tmp):
            os.remove(self._tmp)

    def _downgrade(self, store):
        with store.transaction():
            cur = store.conn.cursor()
            cur.execute("DROP TABLE IF EXISTS user_active_context")
            cur.execute(store.dialect.sql(
                "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))

    def test_upgrade_from_043_and_reopen_preserves_selection(self):
        stamp = datetime(2027, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                self._downgrade(store)                        # adopt a pre-044 DB
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
                migrate(store.conn, store.dialect)            # apply 044
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.set_active_context(ActiveContext(
                    id="user_x", program_id="prog_x", season_id="seas_x",
                    updated_at=stamp))
                store.set_active_context(ActiveContext(       # program-only row
                    id="user_y", program_id="prog_y", season_id=None,
                    updated_at=stamp))
                store.close()
                store = SqlStore(loc)                         # real close/reopen
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                rec = store.get_active_context("user_x")
                self.assertEqual((rec.program_id, rec.season_id, rec.updated_at),
                                 ("prog_x", "seas_x", stamp), label)
                rec = store.get_active_context("user_y")
                self.assertEqual((rec.program_id, rec.season_id),
                                 ("prog_y", None), label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


class ActiveContextHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("DATABASE_URL", None)   # the default in-memory demo store
        STATE.reset()                          # seeds demo Program/Season + accounts
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, opener=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req("POST", "/api/auth/login",
                  {"username": username, "password": "demo"}, opener=op)
        return op

    def test_get_set_roundtrip_carries_read_only(self):
        admin = self._login("admin")
        s, ctx = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(s, 200, ctx)
        self.assertIn("read_only", ctx)
        self.assertIsNotNone(ctx["program_id"], ctx)
        s, out = self._req("POST", "/api/context",
                           {"program_id": ctx["program_id"],
                            "season_id": ctx["season_id"]}, opener=admin)
        self.assertEqual(s, 200, out)
        self.assertEqual(out["season_id"], ctx["season_id"])
        # Program-only selection over HTTP (explicit-null season) is accepted.
        s, out = self._req("POST", "/api/context",
                           {"program_id": ctx["program_id"], "season_id": None},
                           opener=admin)
        self.assertEqual(s, 200, out)
        self.assertIsNone(out["season_id"], out)

    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self._req("GET", "/api/context")[0], 401)
        self.assertEqual(self._req("POST", "/api/context",
                                   {"program_id": "p"})[0], 401)

    def test_strict_body_schema(self):
        admin = self._login("admin")
        # missing required program_id
        s, b = self._req("POST", "/api/context", {}, opener=admin)
        self.assertEqual((s, b["error"]["details"]["reason"]),
                         (400, "field_required"))
        # unknown field
        s, b = self._req("POST", "/api/context",
                         {"program_id": "p", "nope": 1}, opener=admin)
        self.assertEqual((s, b["error"]["details"]["reason"]),
                         (400, "unknown_field"))
        # wrong type
        s, b = self._req("POST", "/api/context", {"program_id": 123}, opener=admin)
        self.assertEqual((s, b["error"]["details"]["reason"]), (400, "wrong_type"))

    def test_selecting_a_context_grants_no_authority(self):
        # A viewer may record its own view selection, but that must not let it
        # perform an operator action — context is orthogonal to authority.
        admin = self._login("admin")
        _, ctx = self._req("GET", "/api/context", opener=admin)
        viewer = self._login("viewer")
        s, _ = self._req("POST", "/api/context",
                         {"program_id": ctx["program_id"],
                          "season_id": ctx["season_id"]}, opener=viewer)
        self.assertEqual(s, 200)                    # selecting a context: allowed
        s, denied = self._req("POST", "/api/setup/venue", {"name": "V"},
                              opener=viewer)
        self.assertEqual(s, 403, denied)            # operator write: still forbidden
        self.assertEqual(denied["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
