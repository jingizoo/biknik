"""TEST HARNESS: run the real demo server with one scoped read parkable INSIDE it.

Used only by ``context-switch-server-exit.js``. It exists so a BROWSER journey
can create the one condition a browser cannot otherwise create — a request that
the server has genuinely ACCEPTED and is still executing — and then drive a real
context switch against it.

WHY THIS IS A SEPARATE FILE AND NOT A HOOK IN THE SERVER. The property under
test is a server-side ordering guarantee; a production flag that let any caller
park a request would be a denial-of-service affordance shipped to make a test
easier. So the seam lives here, in the e2e tree, and is installed by MONKEY-
PATCHING the real ``ApiService`` methods after import. ``hockey_scheduler`` is
byte-identical to what production runs.

The patch wraps the CLASS, not the live instance, so it survives
``STATE.reset()`` rebuilding the ApiService (the demo's Load/Reset controls do
exactly that).

CONTROL PLANE, on ``--control-port`` — plain HTTP, loopback only, never part of
the app:

  POST /arm     {"kind": "venue-candidates"|"venue-access", "season_id": "..."}
                Arm a ONE-SHOT park for the next matching read.
  GET  /status  {"armed":bool,"parked":bool,"released":bool,"exited":bool,
                 "outcome":"ok"|"error:<code>"|null,"season_id":str|null}
                ``outcome`` is what the SERVER's own handler produced for the
                parked read — the direct observation of whether the exact-Season
                ceiling refused it, taken inside the process rather than
                inferred from the wire.
  POST /release Let the parked read run to completion.
  POST /reset   Disarm and clear, for the next leg.
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# This file lives in e2e/ but drives the backend, so `sys.path[0]` is the e2e
# directory rather than the package root. Add the backend explicitly instead of
# relying on the spawner's cwd.
_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from hockey_scheduler.api import ApiService  # noqa: E402
from hockey_scheduler.web import server as app_server  # noqa: E402

_STATE = {
    "armed": None,          # {"kind":..., "season_id":...} or None
    "parked": False,
    "released": False,
    "exited": False,
    "outcome": None,
    "season_id": None,
}
_LOCK = threading.Lock()
_RELEASE = threading.Event()

_METHODS = {
    "venue-candidates": "get_venue_grant_candidates",
    "venue-access": "list_season_venue_access",
}


def _install_park(kind, method_name):
    original = getattr(ApiService, method_name)

    def wrapper(self, season_id, *args, **kwargs):
        park_this = False
        with _LOCK:
            armed = _STATE["armed"]
            if armed and armed["kind"] == kind \
                    and armed["season_id"] == season_id:
                _STATE["armed"] = None       # ONE-SHOT: later reads (including
                _STATE["parked"] = True      # the journey's own ceiling control)
                _STATE["season_id"] = season_id   # are never held.
                park_this = True
        if park_this:
            # The request is now INSIDE the server, holding the context gate's
            # SHARED hold for this user. A switch that arrives from here on must
            # not be able to commit until this returns.
            _RELEASE.wait(60)
            with _LOCK:
                _STATE["released"] = True
        result = original(self, season_id, *args, **kwargs)
        if park_this:
            with _LOCK:
                _STATE["exited"] = True
                if isinstance(result, dict) and "error" in result:
                    _STATE["outcome"] = f"error:{result['error'].get('code')}"
                else:
                    _STATE["outcome"] = "ok"
        return result

    wrapper.__name__ = method_name
    setattr(ApiService, method_name, wrapper)


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] != "/status":
            return self._send({"error": "unknown"}, 404)
        with _LOCK:
            snapshot = dict(_STATE)
        snapshot["armed"] = bool(snapshot["armed"])
        return self._send(snapshot)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send({"error": "bad json"}, 400)
        if path == "/arm":
            kind = body.get("kind")
            if kind not in _METHODS or not body.get("season_id"):
                return self._send({"error": "kind/season_id required"}, 400)
            with _LOCK:
                _STATE.update({"armed": {"kind": kind,
                                         "season_id": body["season_id"]},
                               "parked": False, "released": False,
                               "exited": False, "outcome": None,
                               "season_id": None})
            _RELEASE.clear()
            return self._send({"ok": True})
        if path == "/release":
            _RELEASE.set()
            return self._send({"ok": True})
        if path == "/reset":
            with _LOCK:
                _STATE.update({"armed": None, "parked": False,
                               "released": False, "exited": False,
                               "outcome": None, "season_id": None})
            _RELEASE.set()
            return self._send({"ok": True})
        return self._send({"error": "unknown"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--control-port", type=int, required=True)
    args = parser.parse_args()

    for kind, method_name in _METHODS.items():
        _install_park(kind, method_name)

    control = ThreadingHTTPServer((args.host, args.control_port), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    print(f"park-control on http://{args.host}:{args.control_port}", flush=True)
    sys.stdout.flush()
    app_server.serve(args.host, args.port)


if __name__ == "__main__":
    main()
