"""Server-side request tracing behind HS_REQUEST_TRACE (#465).

The demo-lifecycle browser journey intermittently times out waiting for a
response to a POST it sent. Request-level instrumentation in the browser
masked the failure, so the server can trace each request from its own side
instead. This pins the contract the journey's failure output relies on: off by
default, and when on, ACCEPT, ARRIVE and DONE, in that order, for a real
request.
"""

import contextlib
import io
import re
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as web

_LINE = re.compile(r"^TRACE (\d+\.\d{3}) (ACCEPT|ARRIVE|DONE|RAISED) t=\d+ (.*)$")


class RequestTraceTest(unittest.TestCase):

    def _serve_one(self, enabled, path="/api/health"):
        """Answer one real GET and return the TRACE lines written to stderr."""
        web.STATE.reset()
        captured = io.StringIO()
        with mock.patch.object(web, "_REQUEST_TRACE", enabled), \
                contextlib.redirect_stderr(captured):
            httpd = web.Server(("127.0.0.1", 0), web.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    resp.read()
                # DONE is written after the response, so the client can see
                # the body first. Give the handler thread a moment.
                deadline = time.monotonic() + 3
                while enabled and " DONE " not in captured.getvalue() \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()
        return [line for line in captured.getvalue().splitlines()
                if line.startswith("TRACE ")]

    def test_off_by_default_and_when_unset(self):
        self.assertEqual(self._serve_one(False), [])

    def test_traces_accept_arrive_done_in_order(self):
        lines = self._serve_one(True)
        parsed = [_LINE.match(line) for line in lines]
        self.assertTrue(all(parsed), lines)
        events = [(m.group(2), m.group(3)) for m in parsed]
        self.assertEqual([event for event, _ in events],
                         ["ACCEPT", "ARRIVE", "DONE"], lines)
        self.assertRegex(events[0][1], r"^127\.0\.0\.1:\d+$")
        self.assertEqual(events[1][1], "GET /api/health")
        self.assertRegex(events[2][1], r"^GET /api/health \d+\.\d{3}s$")
        stamps = [float(m.group(1)) for m in parsed]
        self.assertEqual(stamps, sorted(stamps), lines)


if __name__ == "__main__":
    unittest.main()
