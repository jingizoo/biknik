"""Regression + audit-integrity tests for POST /api/demo/add-ice-slot.

Two things are proven here, across Memory / SQLite / PostgreSQL, over real HTTP:

1. **It works on a SqlStore and is server-attributed.** The handler previously
   read `api.store.ice_slots.values()` — an InMemoryStore-only dict attribute —
   so in production (SqlStore, `DATABASE_URL` set) it raised
   `AttributeError: 'SqlStore' object has no attribute 'ice_slots'` (HTTP 500).
   It now uses the portable `all_ice_slots()` accessor. And it attributed every
   `ice_slot_created` audit to a hardcoded `"arena_mgr"` constant instead of the
   real operator; it now names the authenticated caller (CLAUDE.md #5). Two
   different signed-in `MANAGE_ARENA` callers must produce two different actors.

2. **It is unavailable in production.** Like the other `/api/demo/*` controls,
   the route is gated out of production: an authenticated operator gets
   403 `demo_action_not_available`, and header-only / unauthenticated requests
   are rejected (401) — all with zero slot/audit mutation.

Each test drives the real HTTP handler against a store selected via
`DATABASE_URL` (exactly as production selects Postgres) and rebuilds the shared
demo state per backend. Setup/teardown save and restore the environment so the
shared global `STATE` is left clean for other test modules.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.web import server as web

HOST = "127.0.0.1"
PORT = None
_HTTPD = None
_THREAD = None
_TMP_FILES = []
_SAVED_ENV = {}

# Two distinct authenticated MANAGE_ARENA callers, seeded with deterministic ids
# (the same create_account path the demo seed uses for its "arena" persona).
_CALLERS = (("arena_alice", "user_arena_alice"), ("arena_bob", "user_arena_bob"))


def setUpModule():
    global PORT, _HTTPD, _THREAD
    for k in ("DATABASE_URL", "APP_MODE", "DEMO_HEADERLESS_ADMIN"):
        _SAVED_ENV[k] = os.environ.get(k)
    os.environ.pop("APP_MODE", None)            # demo mode by default
    os.environ.pop("DEMO_HEADERLESS_ADMIN", None)
    _HTTPD = ThreadingHTTPServer((HOST, 0), web.Handler)
    PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD:
        _HTTPD.shutdown()
        _HTTPD.server_close()
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        web.STATE.reset(seed=False)   # leave the shared global on a clean store
    except Exception:
        pass
    for path in _TMP_FILES:
        if os.path.exists(path):
            os.remove(path)


def _backends():
    """Yield (label, database_url) per available backend. None → InMemoryStore."""
    yield "memory", None
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _TMP_FILES.append(path)
    yield "sqlite", path
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield "postgres", url


def _use_backend(database_url):
    if database_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = database_url
    web.STATE.reset(seed=True)      # rebuild STATE.api on the selected store


def _request(method, path, body=None, opener=None, role=None):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if role is not None:
        req.add_header("X-Demo-Role", role)
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def _login(username, password="demo"):
    """Log in over HTTP; return a cookie-jar opener carrying the session."""
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
    status, _ = _request("POST", "/api/auth/login",
                         {"username": username, "password": password}, opener=op)
    return op, status


def _ice_slot_created_audits():
    return [a for a in web.STATE.api.store.all_setup_audit()
            if a.action == "ice_slot_created"]


class DemoAddIceSlotTest(unittest.TestCase):
    def test_slot_and_audit_attributed_to_authenticated_caller(self):
        for label, db_url in _backends():
            with self.subTest(backend=label):
                _use_backend(db_url)
                # Premise guard: really on the intended store (else the SqlStore
                # crash / attribution asserts would pass vacuously).
                self.assertEqual(
                    web.STATE.api.store.__class__.__name__,
                    "InMemoryStore" if db_url is None else "SqlStore", label)

                for uname, uid in _CALLERS:
                    web.STATE.api.accounts.create_account(
                        uname, "demo", Role.ARENA_MANAGER, scope={},
                        actor_id="test_seed", account_id=uid)

                slots = {}
                for uname, uid in _CALLERS:
                    op, s = _login(uname)
                    self.assertEqual(s, 200, (label, uname, "login"))
                    # #409: the demo add-ice-slot route mints a real IceSlot
                    # and a real audit row, so it is now gated like the setup
                    # route it shadows. Persist byte-for-byte the tuple this
                    # session already resolves — the Rink and the Program are
                    # exactly the ones this test always used.
                    s, ctx = _request("GET", "/api/context", opener=op)
                    self.assertEqual(s, 200, (label, uname, ctx))
                    if ctx.get("program_id"):
                        s, sel = _request("POST", "/api/context",
                                          {"program_id": ctx.get("program_id"),
                                           "season_id": ctx.get("season_id"),
                                           "league_id": ctx.get("league_id")},
                                          opener=op)
                        self.assertEqual(s, 200, (label, uname, sel))
                    s, body = _request("POST", "/api/demo/add-ice-slot",
                                       {"date": "2027-03-01"}, opener=op)
                    self.assertEqual(s, 200, (label, uname, body))
                    self.assertTrue(body.get("id", "").startswith("slot_"),
                                    (label, body))
                    slots[uid] = body["id"]

                # Two callers → two DISTINCT real actors, each server-attributed
                # to itself on its slot's ice_slot_created audit — never the
                # synthetic "arena_mgr".
                by_slot = {a.entity_id: a for a in _ice_slot_created_audits()}
                for _uname, uid in _CALLERS:
                    audit = by_slot.get(slots[uid])
                    self.assertIsNotNone(audit, (label, uid, "audit missing"))
                    self.assertEqual(audit.actor_id, uid, (label, uid))
                    self.assertNotEqual(audit.actor_id, "arena_mgr", (label, uid))
                self.assertNotEqual(slots["user_arena_alice"],
                                    slots["user_arena_bob"], label)

    def test_production_gate_rejects_with_zero_mutation(self):
        for label, db_url in _backends():
            with self.subTest(backend=label):
                _use_backend(db_url)
                # A real signed-in MANAGE_ARENA session (the seeded persona);
                # in production its cookie is still authoritative, so it reaches
                # the handler and hits the demo gate — proving the gate itself,
                # not merely that auth is required.
                arena, s = _login("arena")
                self.assertEqual(s, 200, label)
                slots_before = len(web.STATE.api.store.all_ice_slots())
                audits_before = len(_ice_slot_created_audits())

                os.environ["APP_MODE"] = "production"
                try:
                    s, body = _request("POST", "/api/demo/add-ice-slot",
                                       {"date": "2027-03-01"}, opener=arena)
                    self.assertEqual(s, 403, (label, "authenticated", body))
                    self.assertEqual(body["error"]["code"],
                                     "demo_action_not_available", label)

                    s, _ = _request("POST", "/api/demo/add-ice-slot",
                                    {"date": "2027-03-01"}, role="arena_manager")
                    self.assertEqual(s, 401, (label, "header-only"))

                    s, _ = _request("POST", "/api/demo/add-ice-slot",
                                    {"date": "2027-03-01"})
                    self.assertEqual(s, 401, (label, "unauthenticated"))
                finally:
                    os.environ.pop("APP_MODE", None)

                # Zero mutation: no slot created, no audit written.
                self.assertEqual(len(web.STATE.api.store.all_ice_slots()),
                                 slots_before, label)
                self.assertEqual(len(_ice_slot_created_audits()),
                                 audits_before, label)


if __name__ == "__main__":
    unittest.main()
