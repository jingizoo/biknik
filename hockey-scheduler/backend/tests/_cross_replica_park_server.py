"""TEST HARNESS (round-N+3 finding 1): the real demo server as a genuinely
INDEPENDENT OS PROCESS -- mirrors ``_cross_replica_server.py`` (round-N+1
finding 3) -- with ONE ADDITIONAL, TEST-ONLY SEAM: a park placed AFTER
``ApiService.set_active_context``'s own write has already committed and
BEFORE its result reaches ``web/server.py``'s response-serialization step
(``_with_context_epoch`` / ``_send_api``). That is the exact seam the
review named: "after replica A commits its context write but before A
serializes/sends its response".

WHY A SEPARATE FILE, NOT A HOOK IN THE SERVER -- the same rule
``e2e/server-park-harness.py`` already states for its own park seam (see
that file's module docstring): the property under test is a server-side
ordering guarantee, and a production flag letting any caller park a write
mid-flight would be a denial-of-service affordance shipped only to make a
test easier. The seam here is installed by MONKEY-PATCHING
``ApiService.set_active_context`` at the CLASS level, AFTER import --
``hockey_scheduler`` itself stays byte-identical to what production runs;
this file's own control plane is the only thing that is test-only.

WHERE THE PARK SITS, PRECISELY. ``ApiService.set_active_context(...,
include_epoch=True)`` returns ``(payload, epoch)`` — and BY THE TIME it
returns, ``ContextService.set_with_league_and_epoch`` has already: acquired
the per-user epoch fence, validated, WRITTEN the row, derived the epoch
from that SAME snapshot, and COMMITTED (``ContextService._snapshot``'s
``with self.store.transaction(...)`` block has exited normally --
see ``services/context_service.py``). So parking *after* the wrapped call
returns is parking strictly after the database commit and strictly before
``web/server.py``'s own ``_with_context_epoch`` dict-attach and
``_send_api`` write to the socket — there is nothing else executing in
between for a regression to hide in, which is exactly what makes this the
seam the review asked for rather than an approximation of it.

THE GROUND-TRUTH CAPTURE. The exact ``(payload, epoch)`` pair the write
produced is recorded into this file's own control-plane state THE INSTANT
the park begins — before the caller (the test) is allowed to let a second
writer for the same account commit. That pair is exposed over ``/status``
as ``captured_payload`` / ``captured_epoch``. A correct implementation
guarantees the eventual HTTP response equals this pair exactly, because
nothing downstream of this seam is allowed to re-derive either half; the
test's job is to prove that holds even when a second replica commits a
real write for the SAME account while this process sits parked right here.

Usage:
    python3 _cross_replica_park_server.py --port <p> --control-port <cp> \
        --database-url <url>

Prints ``READY <port>`` then ``PARK-CONTROL <cp>`` to stdout, each a plain
line (not JSON) — the caller polls stdout for both, order-independent.

CONTROL PLANE, on ``--control-port`` — plain HTTP, loopback only, never
part of the app:

  POST /arm     {"user_id": "..."}
                Arm a ONE-SHOT park for the next ``set_active_context``
                call made for exactly this user_id.
  GET  /status  {"armed": bool, "parked": bool, "released": bool,
                 "captured_epoch": str|null, "captured_payload": {...}|null}
                ``captured_*`` is the write's OWN return value, recorded at
                the instant the park began — the ground truth the test
                checks the eventual HTTP response against.
  POST /release Let the parked write's already-decided result return to
                the HTTP handler, which then builds and sends the response.
  POST /reset   Disarm and clear, for the next leg.
"""

import argparse
import copy
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--control-port", type=int, required=True)
parser.add_argument("--database-url", required=True)
args = parser.parse_args()

# MUST be set before `hockey_scheduler.web.server` is imported: `STATE` (a
# module-level singleton) constructs its store at IMPORT time, reading
# DATABASE_URL then — the same ordering constraint `_cross_replica_server.py`
# already documents and relies on.
os.environ["DATABASE_URL"] = args.database_url

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from hockey_scheduler.api import ApiService  # noqa: E402
from hockey_scheduler.web import server as app_server  # noqa: E402

_STATE = {
    "armed": None,              # target user_id, or None
    "parked": False,
    "released": False,
    "captured_epoch": None,
    "captured_payload": None,
}
_LOCK = threading.Lock()
_RELEASE = threading.Event()


def _install_park():
    """Wrap ``ApiService.set_active_context`` so the park happens strictly
    AFTER the real call (and therefore its commit) returns. Wraps the CLASS,
    not a live instance, matching ``e2e/server-park-harness.py``'s own
    convention, so it survives ``STATE.reset()`` rebuilding the ApiService."""
    original = ApiService.set_active_context

    def wrapper(self, user_id, *rest, **kwargs):
        # The write (and, when include_epoch=True, the same-snapshot epoch
        # derivation and the commit) happens INSIDE this call. By the time it
        # returns, there is nothing left to decide — only whether THIS
        # process pauses before handing the already-decided result back.
        result = original(self, user_id, *rest, **kwargs)
        park_this = False
        with _LOCK:
            if _STATE["armed"] == user_id:
                _STATE["armed"] = None       # ONE-SHOT
                _STATE["parked"] = True
                if isinstance(result, tuple):
                    payload, epoch = result
                else:
                    payload, epoch = result, None
                _STATE["captured_payload"] = copy.deepcopy(payload)
                _STATE["captured_epoch"] = epoch
                park_this = True
        if park_this:
            # Parked with NO store transaction and NO in-process lock this
            # process itself needs for anything else — the commit already
            # happened above. A second replica writing for the SAME account
            # right now is not blocked by anything in this process; only a
            # genuinely independent OS process (this file's whole point) can
            # demonstrate that.
            _RELEASE.wait(60)
            with _LOCK:
                _STATE["released"] = True
        return result

    wrapper.__name__ = "set_active_context"
    ApiService.set_active_context = wrapper


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
            user_id = body.get("user_id")
            if not user_id:
                return self._send({"error": "user_id required"}, 400)
            with _LOCK:
                _STATE.update({"armed": user_id, "parked": False,
                               "released": False, "captured_epoch": None,
                               "captured_payload": None})
            _RELEASE.clear()
            return self._send({"ok": True})
        if path == "/release":
            _RELEASE.set()
            return self._send({"ok": True})
        if path == "/reset":
            with _LOCK:
                _STATE.update({"armed": None, "parked": False,
                               "released": False, "captured_epoch": None,
                               "captured_payload": None})
            _RELEASE.set()
            return self._send({"ok": True})
        return self._send({"error": "unknown"}, 404)


def main():
    _install_park()
    control = ThreadingHTTPServer(("127.0.0.1", args.control_port), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    httpd = app_server.Server(("127.0.0.1", args.port), app_server.Handler)
    print(f"READY {args.port}", flush=True)
    print(f"PARK-CONTROL {args.control_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
