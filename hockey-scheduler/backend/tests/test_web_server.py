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
        # Fresh, fully-confirmed roster before each test, and derive a
        # selected player id from the board (ids depend on the seed).
        _request("POST", "/api/reset", {})
        _, board = _request("GET", "/api/games/game_1/board")
        self.selected_id = next(p["id"] for p in board["players"]
                                if p["group"] == "selected")

    def test_board_ok_and_json_safe(self):
        status, body = _request("GET", "/api/games/game_1/board")
        self.assertEqual(status, 200)
        # start_time serialized as an ISO string over the wire.
        self.assertIsInstance(body["game"]["start_time"], str)

    def test_overview_endpoint_ok(self):
        status, body = _request("GET", "/api/demo/overview")
        self.assertEqual(status, 200)
        self.assertEqual(body["league"]["name"], "Alpine Ice Hockey League")

    def test_unknown_endpoint_404(self):
        status, body = _request("GET", "/api/games/game_1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_already_selected_returns_409(self):
        status, body = _request(
            "POST", "/api/games/game_1/substitutes/enroll",
            {"player_id": self.selected_id},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "already_selected")

    def test_invalid_availability_returns_400(self):
        status, body = _request(
            "POST", "/api/games/game_1/availability",
            {"player_id": self.selected_id, "availability_status": "bad"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_locked_backout_via_availability_returns_409(self):
        _request("POST", "/api/games/game_1/roster/lock", {})
        status, body = _request(
            "POST", "/api/games/game_1/availability",
            {"player_id": self.selected_id, "availability_status": "unavailable"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "roster_locked")

    def test_create_club_via_setup_endpoint(self):
        status, body = _request("POST", "/api/setup/club",
                                {"name": "Eagles HC", "country": "AT"})
        self.assertEqual(status, 200)
        self.assertTrue(body["id"].startswith("club_"))
        self.assertEqual(body["name"], "Eagles HC")

    def test_schedule_game_from_available_slot(self):
        # Find an available slot and the two U16 teams from the overview.
        _, ov = _request("GET", "/api/demo/overview")
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        status, game = _request("POST", "/api/setup/game", {
            "season_id": ov["seasons"][0]["id"],
            "division_id": u16[0]["division_id"],
            "home_team_id": u16[0]["id"], "away_team_id": u16[1]["id"],
            "ice_slot_id": slot["id"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(game["ice_slot_id"], slot["id"])
        # The new game's roster board is reachable via generic routing.
        bstatus, _ = _request("GET", f"/api/games/{game['id']}/board")
        self.assertEqual(bstatus, 200)
        # The slot is now allocated in the overview.
        _, ov2 = _request("GET", "/api/demo/overview")
        allocated = {s["id"] for s in ov2["ice_slots"] if s["status"] == "allocated"}
        self.assertIn(slot["id"], allocated)

    def test_setup_validation_error_returns_400(self):
        status, body = _request("POST", "/api/setup/league", {"name": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_root_serves_web_console(self):
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/") as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("Operator Console", html)
        self.assertIn('class="sidebar"', html)

    def test_mobile_route_serves_phone_shell(self):
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/mobile") as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn('class="phone"', html)


if __name__ == "__main__":
    unittest.main()
