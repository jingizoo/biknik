import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as web


def _request(method, path, body=None):
    """Return (status_code, parsed_json) for a request to the test server."""
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


HOST = "127.0.0.1"
PORT = None
_HTTPD = None
_THREAD = None


def setUpModule():
    global PORT, _HTTPD, _THREAD
    _HTTPD = ThreadingHTTPServer((HOST, 0), web.Handler)
    PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD:
        _HTTPD.shutdown()


class WebServerTest(unittest.TestCase):
    def setUp(self):
        # Fresh, fully-confirmed roster before each test.
        _request("POST", "/api/reset", {})

    def test_board_ok_and_json_safe(self):
        status, body = _request("GET", "/api/games/game_1/board")
        self.assertEqual(status, 200)
        # start_time serialized as an ISO string over the wire.
        self.assertIsInstance(body["game"]["start_time"], str)

    def test_unknown_endpoint_404(self):
        status, body = _request("GET", "/api/games/game_1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_already_selected_returns_409(self):
        status, body = _request(
            "POST", "/api/games/game_1/substitutes/enroll",
            {"player_id": "player_skater_1"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "already_selected")

    def test_invalid_availability_returns_400(self):
        status, body = _request(
            "POST", "/api/games/game_1/availability",
            {"player_id": "player_skater_1", "availability_status": "bad"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_locked_backout_via_availability_returns_409(self):
        _request("POST", "/api/games/game_1/roster/lock", {})
        status, body = _request(
            "POST", "/api/games/game_1/availability",
            {"player_id": "player_skater_1", "availability_status": "unavailable"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "roster_locked")


if __name__ == "__main__":
    unittest.main()
