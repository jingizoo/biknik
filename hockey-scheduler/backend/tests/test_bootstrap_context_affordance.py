"""The FIRST-RUN state, and the fact the switcher needs in order to be honest.

WHAT WAS BROKEN (#411, defect 1). On an installation holding exactly ONE Program
and ZERO Seasons, an operator could not persist an ``ActiveContext`` row through
any shipped control, while every Program-axis create demanded one. The UI half
of that is a browser problem (``e2e/bootstrap-program-selection.js`` drives the
control end to end). This file pins the SERVER-SIDE fact without which no
honest control can be drawn at all.

THE FACT. ``GET /api/context/options`` reports two different things:

    selected  — the resolved, RENDERABLE context. May have been invented by
                ``ContextService._fallback()``.
    saved     — the axes the PERSISTED row actually names, validated exactly as
                ``resolve_saved_with_league`` validates them. This is the
                authority every #409 create/mutation gate is judged against.

In the first-run state the two are VALUE-IDENTICAL on the Program axis while
nothing is persisted at all — ``_fallback()`` walks the caller's authorized
Programs in id order, and with one Program it picks that one every time. So a
client holding only ``selected`` cannot distinguish "you have chosen this" from
"there is only one thing you could have chosen"; it necessarily paints a
selection the very next create will refuse. ``test_the_fallback_and_the_saved_
authority_disagree_before_any_selection`` asserts that coincidence head-on: the
two carry the same Program id, and ``saved`` is null, in the same response.

THE CORRELATION IS THE POINT. ``saved`` is not a decorative flag; the tests
below bracket every assertion about it with the create it predicts, in the same
state, over real HTTP:

    saved.program_id is None  <->  POST /api/v2/setup/season  ->  409
    saved.program_id == P     <->  POST /api/v2/setup/season  ->  200

so a ``saved`` that ever drifted from the gate's own reading would fail here
rather than mislead a UI into hiding its only bootstrap control.

FALSIFIER. Serve ``saved`` from ``resolve_with_league`` (the fallback resolver)
instead of ``resolve_saved_with_league`` — the single most plausible way to
"simplify" this field — and
``test_the_fallback_and_the_saved_authority_disagree_before_any_selection``
fails on all three stores while everything else still passes: exactly the
silent, coincidence-shaped regression the field exists to prevent.

THREE STORES. ``InMemoryStore``, SQLite and PostgreSQL. ``saved`` is read from a
row inside the same snapshot as the options beside it; "is there a row, and does
it still name authorized records" is a dict lookup on one store and real queries
with real NULL semantics on the others. A SKIP IS NOT A PASS — the PostgreSQL
class announces loudly when ``TEST_DATABASE_URL`` is unset.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None


def setUpModule():
    global _HTTPD, _THREAD, _PORT, _SAVED_DATABASE_URL
    _SAVED_DATABASE_URL = os.environ.get("DATABASE_URL")
    _HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    _PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD is not None:
        _HTTPD.shutdown()
        _THREAD.join(timeout=5)
        _HTTPD.server_close()
    if _SAVED_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DATABASE_URL
    try:
        srv.STATE.reset(seed=False)
    except Exception:
        pass
    for path in _TMP_FILES:
        if os.path.exists(path):
            os.remove(path)


def _sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _TMP_FILES.append(path)
    return path


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg missing "
            "— the first-run `saved` authority was NOT exercised on "
            "PostgreSQL. A SKIP HERE IS NOT A PASS: `saved` is derived from a "
            "row read inside the options snapshot, and 'no row' vs 'a row "
            "naming nothing' is exactly the distinction a real database's NULL "
            "semantics can differ on.")


class _FirstRunHarness:
    """The exact first-run world: ONE Program, ZERO Seasons, nobody selected."""

    DATABASE_URL = None       # None -> InMemoryStore

    def setUp(self):
        if self.DATABASE_URL is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.DATABASE_URL
        srv.STATE.reset(seed=False)
        self.store = srv.STATE.api.store
        # Laid down through the setup service with no identity, so none of the
        # #409 gates run while the fixture is built and nobody has selected it.
        self.program = srv.STATE.api.setup.create_program(
            "Alpha Program", timezone_name=TZ).id
        self.assertEqual([p.id for p in self.store.all_programs()],
                         [self.program], "the fixture is not a ONE-Program "
                         "installation, so this is not the first-run state")
        self.assertEqual(list(self.store.all_seasons()), [],
                         "the fixture already holds a Season, so the switcher "
                         "would not be in its collapsed single-entry state")

    def _client(self, jar=None):
        """An opener with its own cookie jar, kept on the opener so a test can
        build a SECOND opener over the SAME session — the server-side analogue
        of the operator reloading the page."""
        jar = jar if jar is not None else CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        opener.cookie_jar = jar
        return opener

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{_PORT}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            return e.code, raw.decode(), json.loads(raw or b"{}")

    def _operator(self):
        """A brand-new League Admin with NO persisted row, asserted — so every
        "nothing is selected" claim below is about this account, not about the
        order the tests happened to run in."""
        username = f"fr_{uuid.uuid4().hex[:10]}"
        account = srv.STATE.api.accounts.create_account(
            username, "demo", Role.LEAGUE_ADMIN)
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(
            self.store.get_active_context(account.id),
            "the fixture operator already has a saved selection")
        return c, account.id

    def _options(self, c):
        status, raw, body = self._req(c, "GET", "/api/context/options")
        self.assertEqual(status, 200, raw)
        return body

    def _create_season(self, c, name):
        return self._req(c, "POST", "/api/v2/setup/season",
                         {"program_id": self.program, "name": name})

    # -- the fact --------------------------------------------------------
    def test_the_fallback_and_the_saved_authority_disagree_before_any_selection(self):
        """One response; the same Program id under `selected`; null under
        `saved`. This is the whole reason `saved` is reported separately."""
        c, _user = self._operator()
        opts = self._options(c)
        self.assertEqual([p["id"] for p in opts["programs"]], [self.program],
                         opts)
        self.assertEqual(opts["programs"][0]["seasons"], [], opts)
        self.assertEqual(
            opts["selected"]["program_id"], self.program,
            "the fallback resolver did not name the only Program, so the "
            "coincidence this test exists to expose is not even present")
        self.assertIsNone(
            opts["saved"]["program_id"],
            "`saved` reported a Program while NOTHING is persisted — it is "
            "being served from the fallback resolver, and a switcher trusting "
            "it would hide the only control that can persist anything")
        self.assertIsNone(opts["saved"]["season_id"], opts)
        self.assertIsNone(opts["saved"]["league_id"], opts)
        # The store agrees: there is no row at all.
        self.assertEqual(len(list(self.store.all_programs())), 1)

    def test_no_selection_means_the_program_axis_create_is_refused(self):
        """The gate's reading of the same state, so `saved: null` is a
        prediction that was checked and not a decoration."""
        c, _user = self._operator()
        self.assertIsNone(self._options(c)["saved"]["program_id"])
        status, raw, body = self._create_season(c, "Season One")
        self.assertEqual(status, 409, raw)
        self.assertEqual(body["error"]["code"], "active_context_required", raw)
        self.assertEqual(list(self.store.all_seasons()), [],
                         "the refused create still wrote a Season")

    def test_an_explicit_selection_flips_both_the_report_and_the_gate(self):
        c, user = self._operator()
        status, raw, echo = self._req(c, "POST", "/api/context",
                                      {"program_id": self.program,
                                       "season_id": None})
        self.assertEqual(status, 200, raw)
        self.assertEqual(echo["program_id"], self.program, raw)
        # The row exists now, and the report follows it.
        self.assertIsNotNone(self.store.get_active_context(user))
        opts = self._options(c)
        self.assertEqual(opts["saved"]["program_id"], self.program, opts)
        self.assertIsNone(opts["saved"]["season_id"], opts)
        self.assertEqual(opts["selected"]["program_id"], self.program, opts)
        # And the create the refusal above named now succeeds.
        status, raw, body = self._create_season(c, "Season One")
        self.assertEqual(status, 200, raw)
        self.assertTrue(body.get("id"), raw)

    def test_the_selection_survives_a_new_connection(self):
        """The persisted row is the authority a browser REFRESH lands on: a
        second, cookie-sharing client that has POSTed nothing of its own reads
        the same `saved` and is granted the same create."""
        c, _user = self._operator()
        self._req(c, "POST", "/api/context",
                  {"program_id": self.program, "season_id": None})
        # A brand-new opener over the SAME session cookie: nothing carried in
        # memory, everything re-read from the server, as after a reload.
        fresh = self._client(jar=c.cookie_jar)
        opts = self._options(fresh)
        self.assertEqual(opts["saved"]["program_id"], self.program, opts)
        status, raw, _ = self._create_season(fresh, "Season After Reload")
        self.assertEqual(status, 200, raw)

    def test_a_saved_season_that_vanished_leaves_the_program_axis_standing(self):
        """`saved` reports VALIDATED axes, not the raw row: a deleted Season
        drops off while the Program the operator chose survives — which is
        exactly what the create gate grants them, so a switcher painted from
        this cannot offer to re-select a Program that is already selected."""
        c, _user = self._operator()
        season = srv.STATE.api.setup.create_season(self.program, "Doomed").id
        self._req(c, "POST", "/api/context",
                  {"program_id": self.program, "season_id": season})
        self.assertEqual(self._options(c)["saved"]["season_id"], season)
        with self.store.transaction():
            self.store.delete_season(season)
        opts = self._options(c)
        self.assertEqual(opts["saved"]["program_id"], self.program, opts)
        self.assertIsNone(opts["saved"]["season_id"], opts)
        # Program-axis create still allowed, on the strength of that Program.
        status, raw, _ = self._create_season(c, "Replacement")
        self.assertEqual(status, 200, raw)

    def test_a_second_operator_does_not_inherit_the_first_ones_selection(self):
        """`saved` is per-account. A shared/global reading would tell operator
        B that the bootstrap is done and hide B's only way to do it."""
        c1, _u1 = self._operator()
        self._req(c1, "POST", "/api/context",
                  {"program_id": self.program, "season_id": None})
        c2, _u2 = self._operator()
        opts = self._options(c2)
        self.assertEqual(opts["selected"]["program_id"], self.program, opts)
        self.assertIsNone(opts["saved"]["program_id"], opts)
        status, raw, body = self._create_season(c2, "B's Season")
        self.assertEqual(status, 409, raw)
        self.assertEqual(body["error"]["code"], "active_context_required", raw)


class FirstRunMemoryTest(_FirstRunHarness, unittest.TestCase):
    DATABASE_URL = None


class FirstRunSqliteTest(_FirstRunHarness, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class FirstRunPostgresTest(_FirstRunHarness, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


if __name__ == "__main__":
    unittest.main()
