import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as web


def _request(method, path, body=None):
    """Return (status_code, parsed_json) for a request to the test server.

    Acts as League Admin via the explicit X-Demo-Role dev header. The old
    headerless admin fallback is now opt-in (DEMO_HEADERLESS_ADMIN), so these
    web-server plumbing tests declare their role explicitly rather than relying
    on an implicit signed-in identity.
    """
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


HOST = "127.0.0.1"
PORT = None
_HTTPD = None
_THREAD = None


def _request_signed_in(method, path, body=None):
    """Like ``_request``, but with a REAL signed-in session rather than the
    identity-less X-Demo-Role header — #367's ``/api/demo/overview`` needs a
    real ``user_id`` to resolve a persisted context from, same requirement
    ``/api/v2/setup/progress``/``/api/context`` already had."""
    url = f"http://{HOST}:{PORT}/api/auth/login"
    data = json.dumps({"username": "admin", "password": "demo"}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        cookie = resp.headers.get("Set-Cookie", "").split(";", 1)[0]
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Cookie": cookie})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def _request_selected(method, path, body=None):
    """Like ``_request_signed_in``, but the session also PERSISTS the context
    it already resolves (#409).

    A guarded CREATE is authorized against the Program/Season the operator
    CHOSE, and an ``X-Demo-Role`` caller has no account to persist a choice
    against. The tuple persisted here is byte-for-byte the one the fallback was
    already handing this session, so the record built below is exactly the one
    this test always built."""
    url = f"http://{HOST}:{PORT}/api/auth/login"
    data = json.dumps({"username": "admin", "password": "demo"}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        cookie = resp.headers.get("Set-Cookie", "").split(";", 1)[0]

    def _call(m, p, b=None):
        u = f"http://{HOST}:{PORT}{p}"
        d = json.dumps(b).encode() if b is not None else None
        r = urllib.request.Request(u, data=d, method=m,
                                   headers={"Cookie": cookie})
        if d is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    _status, ctx = _call("GET", "/api/context")
    if isinstance(ctx, dict) and ctx.get("program_id"):
        _call("POST", "/api/context",
              {"program_id": ctx.get("program_id"),
               "season_id": ctx.get("season_id"),
               "league_id": ctx.get("league_id")})
    return _call(method, path, body)


def setUpModule():
    global PORT, _HTTPD, _THREAD
    _HTTPD = ThreadingHTTPServer((HOST, 0), web.Handler)
    PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD:
        _HTTPD.shutdown()
        _HTTPD.server_close()


class WebServerTest(unittest.TestCase):
    def setUp(self):
        # Fresh, fully-confirmed roster before each test, and derive a
        # selected player id from the board (ids depend on the seed).
        _request("POST", "/api/reset", {"confirm": "RESET"})
        _, board = _request("GET", "/api/games/game_1/board")
        self.selected_id = next(p["id"] for p in board["players"]
                                if p["group"] == "selected")

    def test_board_ok_and_json_safe(self):
        status, body = _request("GET", "/api/games/game_1/board")
        self.assertEqual(status, 200)
        # start_time serialized as an ISO string over the wire.
        self.assertIsInstance(body["game"]["start_time"], str)

    def test_overview_endpoint_ok(self):
        status, body = _request_signed_in("GET", "/api/demo/overview")
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
        # Find an available slot and two teams REGISTERED in U16 Elite (#180 —
        # participation comes from registrations, not the legacy Team.division_id).
        _, ov = _request_signed_in("GET", "/api/demo/overview")
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16_div = next(d for d in ov["divisions"] if d["name"] == "U16 Elite")
        u16 = [r for r in ov["registrations"] if r["division_id"] == u16_div["id"]]
        status, game = _request_selected("POST", "/api/setup/game", {
            "season_id": u16_div["season_id"],
            "division_id": u16_div["id"],
            "home_team_id": u16[0]["team_id"], "away_team_id": u16[1]["team_id"],
            "ice_slot_id": slot["id"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(game["ice_slot_id"], slot["id"])
        # The new game's roster board is reachable via generic routing.
        bstatus, _ = _request("GET", f"/api/games/{game['id']}/board")
        self.assertEqual(bstatus, 200)
        # The slot is now allocated in the overview.
        _, ov2 = _request_signed_in("GET", "/api/demo/overview")
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

    def test_mobile_route_serves_web_console(self):
        # /mobile used to serve a second, divergent HTML file (a decorative
        # phone-bezel mockup) that drifted out of sync with the real console
        # and dead-ended when signed out (#118). index.html is already a
        # single responsive shell, so /mobile now serves it directly — one
        # file, forever in sync, real phones just render it at their own
        # width.
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/mobile") as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("Operator Console", html)
        self.assertIn('class="sidebar"', html)
        self.assertIn('id="login-screen"', html)


if __name__ == "__main__":
    unittest.main()
