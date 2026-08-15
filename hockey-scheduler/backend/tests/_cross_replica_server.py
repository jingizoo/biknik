"""TEST HARNESS (finding 3): run the real demo server as a genuinely
INDEPENDENT OS PROCESS, so ``services/context_gate.py``'s ``CONTEXT_GATE``/
``LIFECYCLE_GATE`` and ``web/server.py``'s ``SESSIONS`` — all Python
module-level singletons — CANNOT be shared with any other replica, exactly
matching what a real second replica in a real deployment would be. Mirrors
``e2e/server-park-harness.py``'s own "run the real, unmodified app as a
subprocess" convention (that file's own docstring: "hockey_scheduler is
byte-identical to what production runs") — this file adds no park/control
seam because finding 3's test needs none: a deterministic A->B->A ordering
is achieved by driving REAL, SEQUENTIAL, SYNCHRONOUS HTTP calls (a client
waits for a response before the next request is sent), which needs no
in-process instrumentation to control.

Usage:
    python3 _cross_replica_server.py --port <p> --database-url <url>

Prints ``READY <port>`` to stdout once the socket is listening (a plain
line, not JSON — the caller polls stdout for it) and then serves forever
until killed.
"""

import argparse
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--database-url", required=True)
args = parser.parse_args()

# MUST be set before `hockey_scheduler.web.server` is imported: `STATE`
# (a module-level singleton) constructs its store at IMPORT time, reading
# DATABASE_URL then — exactly the same ordering constraint
# `test_context_switch_server_exit.py`'s own `ContextGateFixtureBase.
# setUpClass` already documents and relies on.
os.environ["DATABASE_URL"] = args.database_url

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from hockey_scheduler.web import server as app_server  # noqa: E402

if __name__ == "__main__":
    httpd = app_server.Server(("127.0.0.1", args.port), app_server.Handler)
    print(f"READY {args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
