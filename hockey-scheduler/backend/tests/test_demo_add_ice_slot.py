"""Regression: /api/demo/add-ice-slot must work on a SqlStore-backed server.

Production runs on a SqlStore (DATABASE_URL is set). The demo "add ice slot"
quick action read ``api.store.ice_slots.values()`` — an InMemoryStore-only dict
attribute that SqlStore does not expose — so in production the request raised
``AttributeError: 'SqlStore' object has no attribute 'ice_slots'`` and the
handler crashed (HTTP 500). The InMemoryStore-backed demo server never hit it,
so the existing web-server tests could not catch it.

This drives the real HTTP handler against a SqlStore (a file-backed SQLite
database, selected via DATABASE_URL exactly as production selects Postgres) and
asserts the endpoint succeeds. The handler now uses the portable
``all_ice_slots()`` accessor, which both stores implement.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as web

HOST = "127.0.0.1"
PORT = None
_HTTPD = None
_THREAD = None
_TMP_DB = None
_SAVED_DB_URL = None


def setUpModule():
    global PORT, _HTTPD, _THREAD, _TMP_DB, _SAVED_DB_URL
    # Point the demo server at a real SqlStore (SQLite file) — the production
    # configuration that surfaced the bug. create_store() reads DATABASE_URL, so
    # STATE.reset() (triggered by /api/reset below) rebuilds STATE.api on it.
    fd, _TMP_DB = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _SAVED_DB_URL = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _TMP_DB
    _HTTPD = ThreadingHTTPServer((HOST, 0), web.Handler)
    PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD:
        _HTTPD.shutdown()
    # Restore the environment and rebuild the shared global STATE on the default
    # (in-memory) store so later test modules are unaffected by this one.
    if _SAVED_DB_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DB_URL
    try:
        web.STATE.reset(seed=False)
    except Exception:
        pass
    if _TMP_DB and os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)


def _request(method, path, body=None):
    """Return (status_code, parsed_json) for a request to the test server,
    acting as League Admin via the explicit dev role header."""
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Demo-Role", "league_admin")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


class DemoAddIceSlotSqlStoreTest(unittest.TestCase):
    def setUp(self):
        # A full demo seed on the SqlStore populates ice slots and
        # STATE.ids["main_rink_id"] — the rink the endpoint defaults to.
        status, _ = _request("POST", "/api/reset", {"confirm": "RESET"})
        self.assertEqual(status, 200)
        # Premise guard: the server really is on a SqlStore. Without this the
        # test could pass vacuously (InMemoryStore exposes .ice_slots).
        self.assertEqual(web.STATE.api.store.__class__.__name__, "SqlStore")

    def test_add_ice_slot_succeeds_on_sqlstore(self):
        # Before the fix this returned a crash (AttributeError → HTTP 500);
        # now the handler reads all_ice_slots() and creates the slot.
        status, body = _request("POST", "/api/demo/add-ice-slot",
                                {"date": "2027-03-01"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("error", body or {})
        self.assertTrue((body or {}).get("id", "").startswith("slot_"), body)


if __name__ == "__main__":
    unittest.main()
