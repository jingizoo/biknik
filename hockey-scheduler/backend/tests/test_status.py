import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import (
    email_transport_from_env,
    push_transport_from_env,
)
from hockey_scheduler.store import SqlStore
from hockey_scheduler.web import server as srv


class RuntimeStatusFacadeTest(unittest.TestCase):
    def test_memory_store_reports_memory(self):
        status = ApiService().runtime_status()
        self.assertEqual(status["store"], "memory")
        self.assertEqual(status["email_mode"], "dry_run")
        self.assertEqual(status["push_mode"], "dry_run")
        # Non-sensitive only — never leaks accounts/secrets.
        self.assertNotIn("password", json.dumps(status).lower())

    def test_sqlite_store_reports_sqlite(self):
        self.assertEqual(ApiService(SqlStore(":memory:")).runtime_status()["store"],
                         "sqlite")

    def test_reflects_configured_delivery_modes(self):
        api = ApiService(
            email_transport=email_transport_from_env(
                {"EMAIL_MODE": "smtp", "SMTP_HOST": "mail.example"}),
            push_transport=push_transport_from_env(
                {"PUSH_MODE": "live", "PUSH_ENDPOINT": "https://push.example/send"}))
        status = api.runtime_status()
        self.assertEqual(status["email_mode"], "smtp")
        self.assertEqual(status["push_mode"], "live")


class StatusEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read() or b"{}")

    def test_status_is_public_and_reports_demo_mode(self):
        # No auth header/cookie — status is a non-sensitive health-style route.
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["app_mode"], "demo")
        self.assertIn(body["store"], ("memory", "sqlite", "postgres"))
        self.assertEqual(body["email_mode"], "dry_run")
        self.assertEqual(body["push_mode"], "dry_run")


class ProductionStatusEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["APP_MODE"] = "production"
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        os.environ.pop("APP_MODE", None)
        srv.STATE.reset()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read() or b"{}")

    def test_status_reports_production_without_a_session(self):
        # Production requires auth for app data, but the status chip endpoint
        # stays reachable so the login screen can show the real posture.
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["app_mode"], "production")


if __name__ == "__main__":
    unittest.main()
