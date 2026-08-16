"""TEST HARNESS: run the real demo server with one scoped read parkable INSIDE it.

Used by ``context-switch-server-exit.js`` and ``context-switch-late-arrival.js``.
It exists so a BROWSER journey can create the two conditions a browser cannot
otherwise create on demand, and then drive a real context switch against each:

  * SERVICE PARK (``/arm``) — a request the server has genuinely ACCEPTED and
    is still executing. Wraps ``ApiService`` and blocks INSIDE the handler,
    below the context gate's arrival ticket.
  * ARRIVAL PARK (``/arm-late``) — a request that has been DISPATCHED but has
    NOT YET ARRIVED at ``do_GET``: the interval owned by the browser queue and
    the wire. Wraps ``Handler.do_GET`` and blocks at the TOP of it, strictly
    ABOVE ``CONTEXT_GATE.arrive()``, so for as long as it is held the gate's
    view of the world is byte-identical to the request not existing. That is
    the interleaving #415's arrival ordering cannot cover and the CONTEXT
    EPOCH exists for (services/context_epoch.py).

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
  POST /arm-late {"path": "/api/v2/setup/seasons/<id>/venue-candidates"}
                Arm a ONE-SHOT ARRIVAL park for the next GET of exactly that
                path. Path-exact and one-shot so the switch's own reads and the
                journey's ceiling controls run unimpeded.
  GET  /status  {"armed":bool,"parked":bool,"released":bool,"exited":bool,
                 "outcome":"ok"|"error:<code>"|null,"season_id":str|null,
                 "late_armed":bool,"late_parked":bool,"late_released":bool,
                 "late_exited":bool,"late_status":int|null,
                 "late_service_called":bool}
                ``outcome`` is what the SERVER's own handler produced for the
                parked read — the direct observation of whether the exact-Season
                ceiling refused it, taken inside the process rather than
                inferred from the wire.
                ``late_status`` is the HTTP status the handler SENT for the
                late-arriving read, and ``late_service_called`` whether it
                reached ``ApiService`` at all. Both are read inside the process,
                which matters here: an aborted fetch may have taken its socket
                with it, so the wire cannot always be asked what the server
                answered — but the server always knows.
  POST /release Let the parked read run to completion.
  POST /release-late  Let the late-arriving read proceed into do_GET.
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
    # -- the ARRIVAL park (see the module docstring) ---------------------
    "late_armed": None,     # exact request path, or None
    "late_parked": False,
    "late_released": False,
    "late_exited": False,
    "late_status": None,        # what the handler SENT for that read
    "late_service_called": False,   # did it reach ApiService at all?
}
_LOCK = threading.Lock()
_RELEASE = threading.Event()
_LATE_RELEASE = threading.Event()
# Marks the ONE thread currently serving the late-arriving read, so the status
# and service probes below attribute what they see to that request and to no
# other. A ThreadingHTTPServer serves each connection on its own thread, so
# thread-local is exactly the right scope; a global flag would be claimed by
# whatever else the page happens to be fetching at the same moment.
_LATE = threading.local()


_METHODS = {
    "venue-candidates": "get_venue_grant_candidates",
    "venue-access": "list_season_venue_access",
}


def _install_park(kind, method_name):
    original = getattr(ApiService, method_name)

    def wrapper(self, season_id, *args, **kwargs):
        # THE NEGATIVE OBSERVATION the late-arrival journey needs: a read that
        # was DISCARDED must never have reached the service, because that is
        # what makes "the ceiling was not evaluated for it" a measurement
        # rather than an inference about response codes.
        if getattr(_LATE, "active", False):
            with _LOCK:
                _STATE["late_service_called"] = True
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


def _install_arrival_park():
    """Block a GET at the TOP of ``do_GET``, ABOVE ``CONTEXT_GATE.arrive()``.

    That placement is the whole instrument. While the request is held the gate
    holds no ticket for it, so a switch arriving in the meantime waits for
    nothing and commits — which is exactly what happens when a browser has
    dispatched a read that is still sitting in its own queue. Parking any lower
    would re-create #415's "already inside the server" case, which is a
    different interleaving and is already covered by ``context-switch-server-
    exit.js``.
    """
    original = app_server.Handler.do_GET

    def wrapper(self):
        path = self.path.split("?", 1)[0]
        with _LOCK:
            armed = _STATE["late_armed"]
            park_this = bool(armed) and path == armed
            if park_this:
                _STATE["late_armed"] = None     # ONE-SHOT
                _STATE["late_parked"] = True
        if not park_this:
            return original(self)
        _LATE.active = True
        try:
            _LATE_RELEASE.wait(60)
            with _LOCK:
                _STATE["late_released"] = True
            return original(self)
        finally:
            _LATE.active = False
            with _LOCK:
                _STATE["late_exited"] = True

    wrapper.__name__ = "do_GET"
    app_server.Handler.do_GET = wrapper


def _install_status_probe():
    """Record the status the handler SENT for the late-arriving read.

    Needed because the journey often cannot read it off the wire: the client
    aborted that fetch, so Chromium may have taken the socket down before the
    answer was written. The server always knows what it produced, and that is
    the fact the assertion is about.
    """
    original = app_server.Handler.send_response

    def wrapper(self, code, *args, **kwargs):
        if getattr(_LATE, "active", False):
            with _LOCK:
                if _STATE["late_status"] is None:   # the first one is the answer
                    _STATE["late_status"] = code
        return original(self, code, *args, **kwargs)

    wrapper.__name__ = "send_response"
    app_server.Handler.send_response = wrapper


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
        snapshot["late_armed"] = bool(snapshot["late_armed"])
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
        if path == "/arm-late":
            target = body.get("path")
            if not target or not str(target).startswith("/"):
                return self._send({"error": "path required"}, 400)
            with _LOCK:
                _STATE.update({"late_armed": str(target),
                               "late_parked": False, "late_released": False,
                               "late_exited": False, "late_status": None,
                               "late_service_called": False})
            _LATE_RELEASE.clear()
            return self._send({"ok": True})
        if path == "/release":
            _RELEASE.set()
            return self._send({"ok": True})
        if path == "/release-late":
            _LATE_RELEASE.set()
            return self._send({"ok": True})
        if path == "/reset":
            with _LOCK:
                _STATE.update({"armed": None, "parked": False,
                               "released": False, "exited": False,
                               "outcome": None, "season_id": None,
                               "late_armed": None, "late_parked": False,
                               "late_released": False, "late_exited": False,
                               "late_status": None,
                               "late_service_called": False})
            _RELEASE.set()
            _LATE_RELEASE.set()
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
    _install_arrival_park()
    _install_status_probe()

    control = ThreadingHTTPServer((args.host, args.control_port), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    print(f"park-control on http://{args.host}:{args.control_port}", flush=True)
    sys.stdout.flush()
    app_server.serve(args.host, args.port)


if __name__ == "__main__":
    main()
