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

from datetime import datetime, timedelta

from ..api import ApiService
from ..full_demo import build_full_demo_store

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
        # Build the full Alpine league/arena scenario via the real setup
        # service, with one game rostered and confirmed, ready to demo a
        # back-out → substitute flow.
        store, game_id, ids = build_full_demo_store()
        self.api = ApiService(store)
        self.game_id = game_id
        self.ids = ids


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
        if path in ("/", ""):
            rel = "index.html"            # desktop web console
        elif path in ("/mobile", "/mobile/"):
            rel = "mobile.html"           # iPhone-framed preview
        else:
            rel = path.lstrip("/")
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
        api = STATE.api
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/demo/overview":
            return self._send_api(api.get_demo_overview())
        # /api/games/{gid}/<sub>  — works for any game id, not just the seed.
        m = re.match(r"^/api/games/([^/]+)(?:/(board|roster-status|roster|substitutes))?$", path)
        if m:
            gid, sub = m.group(1), m.group(2)
            if sub == "board":
                return self._send_api(api.get_board(gid))
            if sub == "roster-status":
                return self._send_api(api.get_roster_status(gid))
            if sub == "roster":
                return self._send_api(api.get_roster(gid))
            if sub == "substitutes":
                return self._send_api(api.get_substitutes(gid))
            if sub is None:
                return self._send_api(api.get_game(gid))
        if path.startswith("/api/"):
            return self._send_json({"error": {"code": "not_found",
                                              "message": "Unknown endpoint."}}, 404)
        return self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        api = STATE.api
        body = self._read_body()
        pid: Optional[str] = body.get("player_id")
        actor = body.get("actor_id", "demo")

        if path == "/api/reset":
            STATE.reset()
            return self._send_json({"ok": True})

        # Quick action: add an available 90-min game slot on a rink for the
        # given date, after the latest existing slot on that rink that day.
        if path == "/api/demo/add-ice-slot":
            rink_id = body.get("rink_id") or STATE.ids["main_rink_id"]
            date = body.get("date")  # "YYYY-MM-DD"
            ends = [s.end_time for s in api.store.ice_slots.values()
                    if s.rink_id == rink_id
                    and (not date or s.start_time.isoformat().startswith(date))]
            if ends:
                start = max(ends) + timedelta(minutes=30)
            elif date:
                start = datetime.fromisoformat(f"{date}T18:00:00+00:00")
            else:
                return self._send_api({"error": {"code": "validation_error",
                                                 "message": "No reference slot."}})
            end = start + timedelta(minutes=90)
            return self._send_api(api.create_ice_slot(
                rink_id, start.isoformat(), end.isoformat(),
                body.get("slot_type", "game"), actor_id="arena_mgr"))

        # Setup create endpoints — operator creates real records via the API.
        if path.startswith("/api/setup/"):
            return self._handle_setup(path[len("/api/setup/"):], body, actor)

        # /api/games/{gid}/<action>
        m = re.match(r"^/api/games/([^/]+)/(.+)$", path)
        if m:
            gid, action = m.group(1), m.group(2)
            if action == "availability":
                return self._send_api(api.set_availability(
                    gid, pid, body.get("availability_status", "pending"),
                    body.get("response_source", "player"), actor))
            if action == "build-roster":
                return self._send_api(api.auto_build_roster(gid, actor))
            if action == "publish":
                return self._send_api(api.publish_game(gid, actor))
            if action == "substitutes/enroll":
                return self._send_api(api.enroll_substitute(gid, pid, actor))
            if action == "substitutes/withdraw":
                return self._send_api(api.withdraw_substitute(gid, pid, actor))
            sub = re.match(r"^substitutes/([^/]+)/(offer|accept|decline|add-to-roster)$", action)
            if sub:
                player_id, op = sub.group(1), sub.group(2)
                if op == "offer":
                    return self._send_api(api.offer_substitute(
                        gid, player_id, actor, expires_at=body.get("expires_at")))
                fn = {"accept": api.accept_substitute,
                      "decline": api.decline_substitute,
                      "add-to-roster": api.add_substitute_to_roster}[op]
                return self._send_api(fn(gid, player_id, actor))
            coach = {"roster/lock": api.lock_roster,
                     "roster/unlock": api.unlock_roster,
                     "cancel": api.cancel_game}.get(action)
            if coach:
                return self._send_api(coach(gid, actor))

        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown endpoint."}}, 404)

    def _handle_setup(self, entity: str, body: dict, actor: str):
        """Dispatch /api/setup/<entity> to the matching facade create method."""
        api = STATE.api
        b = body
        if entity == "league":
            return self._send_api(api.create_league(
                b.get("name"), b.get("country", ""), b.get("timezone", "UTC"), actor))
        if entity == "season":
            return self._send_api(api.create_season(
                b.get("league_id"), b.get("name"),
                b.get("start_date"), b.get("end_date"), actor))
        if entity == "division":
            return self._send_api(api.create_division(
                b.get("season_id"), b.get("name"), b.get("age_group", ""), actor))
        if entity == "club":
            return self._send_api(api.create_club(
                b.get("name"), b.get("country", ""), actor))
        if entity == "team":
            return self._send_api(api.create_team(
                b.get("club_id"), b.get("division_id"), b.get("name"), actor))
        if entity == "venue":
            return self._send_api(api.create_venue(
                b.get("name"), b.get("address", ""), b.get("timezone", "UTC"), actor))
        if entity == "rink":
            return self._send_api(api.create_rink(
                b.get("venue_id"), b.get("name"), actor))
        if entity == "ice-slot":
            return self._send_api(api.create_ice_slot(
                b.get("rink_id"), b.get("start_time"), b.get("end_time"),
                b.get("slot_type", "game"), actor))
        if entity == "game":
            return self._send_api(api.create_game(
                b.get("season_id"), b.get("division_id"), b.get("home_team_id"),
                b.get("away_team_id"), b.get("ice_slot_id"),
                allow_division_override=bool(b.get("allow_division_override")),
                actor_id=actor))
        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown setup entity."}}, 404)


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
