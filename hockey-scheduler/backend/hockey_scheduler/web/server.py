"""Zero-dependency HTTP server for the iPhone-framed demo.

    python3 -m hockey_scheduler.web

Then open http://localhost:8000 in any browser (works on Windows — no Mac or
Xcode needed). The page renders an iPhone frame and drives the *real* roster /
substitute engine through the same :class:`ApiService` used by the tests.
"""

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from ..api import ApiService
from ..domain import AvailabilityStatus
from ..seed import build_seeded_store

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# Maps structured domain-error codes to HTTP status codes (per api-contract.md).
ERROR_HTTP_STATUS = {
    "not_found": 404,
    "validation_error": 400,
    "roster_locked": 409,
    "already_selected": 409,
    "not_enrolled": 409,
    "invalid_transition": 409,
    "slot_already_filled": 409,
    "not_eligible": 403,
    "game_cancelled": 409,
}


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class DemoState:
    """Holds the seeded game + facade; can reset itself for the demo."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        store, game_id = build_seeded_store()
        self.api = ApiService(store)
        self.game_id = game_id
        # Pre-select 1 goalie + 15 skaters and confirm them, so the demo opens
        # on a confirmed roster ready for someone to back out.
        selected = ["player_goalie_1"] + [f"player_skater_{i}" for i in range(1, 16)]
        self.api.select_roster(game_id, selected, actor_id="coach_1")
        for pid in selected:
            self.api.set_availability(game_id, pid, "available")


STATE = DemoState()


class Handler(BaseHTTPRequestHandler):
    # Quieter logging.
    def log_message(self, *args):  # noqa: D401
        pass

    # -- helpers -----------------------------------------------------------
    def _send_json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_api(self, payload) -> None:
        """Send an API payload, mapping structured domain errors to HTTP codes."""
        if isinstance(payload, dict) and "error" in payload:
            code = payload["error"].get("code", "domain_error")
            return self._send_json(payload, ERROR_HTTP_STATUS.get(code, 400))
        return self._send_json(payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self.send_error(404, "Not found")
            return
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        gid = STATE.game_id
        api = STATE.api
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == f"/api/games/{gid}/board":
            return self._send_api(api.get_board(gid))
        if path == f"/api/games/{gid}/roster-status":
            return self._send_api(api.get_roster_status(gid))
        if path == f"/api/games/{gid}/roster":
            return self._send_api(api.get_roster(gid))
        if path == f"/api/games/{gid}/substitutes":
            return self._send_api(api.get_substitutes(gid))
        if path == f"/api/games/{gid}":
            return self._send_api(api.get_game(gid))
        if path.startswith("/api/"):
            return self._send_json({"error": {"code": "not_found",
                                              "message": "Unknown endpoint."}}, 404)
        return self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        gid = STATE.game_id
        api = STATE.api
        body = self._read_body()
        pid: Optional[str] = body.get("player_id")
        actor = body.get("actor_id", "demo")

        if path == "/api/reset":
            STATE.reset()
            return self._send_json({"ok": True})

        # availability (confirm / back out)
        if path == f"/api/games/{gid}/availability":
            return self._send_api(api.set_availability(
                gid, pid, body.get("availability_status", "pending"),
                body.get("response_source", "player"), actor))

        # substitute actions
        sub_routes = {
            f"/api/games/{gid}/substitutes/enroll": api.enroll_substitute,
            f"/api/games/{gid}/substitutes/withdraw": api.withdraw_substitute,
        }
        if path in sub_routes:
            return self._send_api(sub_routes[path](gid, pid, actor))

        m = re.match(rf"^/api/games/{re.escape(gid)}/substitutes/([^/]+)/(offer|accept|decline|add-to-roster)$", path)
        if m:
            player_id, action = m.group(1), m.group(2)
            if action == "offer":
                return self._send_api(api.offer_substitute(
                    gid, player_id, actor, expires_at=body.get("expires_at")))
            fn = {
                "accept": api.accept_substitute,
                "decline": api.decline_substitute,
                "add-to-roster": api.add_substitute_to_roster,
            }[action]
            return self._send_api(fn(gid, player_id, actor))

        # coach controls
        coach_routes = {
            f"/api/games/{gid}/roster/lock": api.lock_roster,
            f"/api/games/{gid}/roster/unlock": api.unlock_roster,
            f"/api/games/{gid}/cancel": api.cancel_game,
        }
        if path in coach_routes:
            return self._send_api(coach_routes[path](gid, actor))

        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown endpoint."}}, 404)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Hockey Scheduler demo running at http://{host}:{port}")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hockey Scheduler demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)
