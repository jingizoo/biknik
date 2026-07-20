"""Season archive/read-only lifecycle (#159 Slice 1).

A Season gains an explicit lifecycle state (``active`` | ``archived``). Archiving
turns it into a read-only historical record: no new registrations, venue access,
Leagues, Divisions, Games, or roll-forward-target rows may be written until an
authorized, *reasoned* reopen. Archived Seasons stay fully readable and retain
all prior history. Transitions are audited. Covered on Memory, SQLite, and
PostgreSQL, plus the HTTP contract (authz + reason + strict body + write-guard).
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.domain.setup_models import Program, Season
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.server import STATE, Handler


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _fixture(api, season_name="S1"):
    """Program → Season → League → Division → Club → two registered Teams →
    Venue, all built through the real (guarded) create paths while active."""
    pid = api.create_program("Prog", "US", "UTC")["id"]
    sid = api.create_season(pid, season_name)["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    t1 = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
    t2 = api.create_team(club_id=club, name="Bravo", league_id=lid)["id"]
    api.register_team_for_season(sid, t1, division_id=did)
    api.register_team_for_season(sid, t2, division_id=did)
    ven = api.create_venue("Rink")["id"]
    return {"pid": pid, "sid": sid, "lid": lid, "did": did, "club": club,
            "t1": t1, "t2": t2, "ven": ven}


def _season_audits(store):
    return [a.action for a in store.all_setup_audit()
            if a.action in ("season_archived", "season_reopened")]


class SeasonLifecycleTest(unittest.TestCase):
    # -- lifecycle transitions + audit --------------------------------------
    def test_archive_then_reopen_roundtrip(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api)
            sid = fx["sid"]
            self.assertEqual(store.get_season(sid).status, SeasonStatus.ACTIVE, label)

            arch = api.setup.archive_season(sid, actor_id="admin", reason="season over")
            self.assertEqual(arch.status, SeasonStatus.ARCHIVED, label)
            self.assertIsNotNone(arch.archived_at, label)
            # DTO exposes the lifecycle state as a plain string.
            self.assertEqual(api.get_setup_overview_v2()["seasons"][0]["status"],
                             "archived", label)

            reopen = api.setup.reopen_season(sid, actor_id="admin", reason="correction")
            self.assertEqual(reopen.status, SeasonStatus.ACTIVE, label)
            self.assertIsNone(reopen.archived_at, label)
            self.assertEqual(_season_audits(store),
                             ["season_archived", "season_reopened"], label)

    def test_reopen_requires_reason(self):
        for label, store in _backends():
            api = ApiService(store)
            sid = _fixture(api)["sid"]
            api.setup.archive_season(sid, actor_id="admin")
            for bad in (None, "", "   "):
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.reopen_season(sid, actor_id="admin", reason=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "reason_required", (label, bad))
            self.assertEqual(store.get_season(sid).status,
                             SeasonStatus.ARCHIVED, label)  # unchanged

    def test_double_transitions_rejected(self):
        for label, store in _backends():
            api = ApiService(store)
            sid = _fixture(api)["sid"]
            api.setup.archive_season(sid, actor_id="admin")
            with self.assertRaises(ValidationError) as ctx:
                api.setup.archive_season(sid, actor_id="admin")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_already_archived", label)
            api.setup.reopen_season(sid, actor_id="admin", reason="x")
            with self.assertRaises(ValidationError) as ctx:
                api.setup.reopen_season(sid, actor_id="admin", reason="x")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_not_archived", label)

    # -- read-only enforcement (zero mutation) ------------------------------
    def test_archived_season_blocks_writes(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api)
            sid = fx["sid"]
            regs0 = len(store.registrations_for_season(sid))
            divs0 = len(store.all_divisions())
            ls0 = len(store.all_league_seasons())
            sva0 = len(store.all_season_venue_access())
            games0 = len(store.all_games())
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            def blocked(fn, note):
                with self.assertRaises(ValidationError) as ctx:
                    fn()
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "season_archived", (label, note))

            blocked(lambda: api.setup.register_team_for_season(
                sid, fx["t1"], division_id=fx["did"]), "register")
            blocked(lambda: api.setup.create_division(sid, "D2"), "division")
            blocked(lambda: api.setup.create_league(sid, "Silver"), "league")
            blocked(lambda: api.setup.create_league_season(fx["lid"], sid),
                    "league_season")
            blocked(lambda: api.setup.grant_season_venue_access(sid, fx["ven"]),
                    "venue_access")
            # create_game guards first, before any team/slot resolution.
            blocked(lambda: api.setup.create_game(
                sid, fx["did"], fx["t1"], fx["t2"], "slot_x"), "game")

            # Nothing was written by any blocked call.
            self.assertEqual(len(store.registrations_for_season(sid)), regs0, label)
            self.assertEqual(len(store.all_divisions()), divs0, label)
            self.assertEqual(len(store.all_league_seasons()), ls0, label)
            self.assertEqual(len(store.all_season_venue_access()), sva0, label)
            self.assertEqual(len(store.all_games()), games0, label)

    def test_rollforward_into_archived_target_blocked(self):
        for label, store in _backends():
            api = ApiService(store)
            src = _fixture(api, season_name="From")
            # A second Season in the SAME program, archived, as the target.
            dst_sid = api.create_season(src["pid"], "To")["id"]
            api.setup.archive_season(dst_sid, actor_id="admin", reason="closed")
            regs0 = len(store.registrations_for_season(dst_sid))
            with self.assertRaises(ValidationError) as ctx:
                api.setup.roll_forward_registrations(src["sid"], dst_sid,
                                                     actor_id="admin")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertEqual(len(store.registrations_for_season(dst_sid)),
                             regs0, label)  # zero copied into the archived target

    def test_archived_history_readable_and_other_seasons_writable(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api, season_name="Archived")
            other = _fixture(api, season_name="Live")
            api.setup.archive_season(fx["sid"], actor_id="admin", reason="done")

            # Archived Season's history is intact and still readable.
            self.assertEqual(len(store.registrations_for_season(fx["sid"])), 2, label)
            self.assertIsNotNone(store.get_season(fx["sid"]), label)
            names = {s["name"]: s["status"]
                     for s in api.get_setup_overview_v2()["seasons"]}
            self.assertEqual(names["Archived"], "archived", label)
            self.assertEqual(names["Live"], "active", label)

            # A different active Season is unaffected — writes still succeed.
            api.setup.create_division(other["sid"], "D2")  # no raise
            self.assertTrue(any(d.name == "D2" for d in store.all_divisions()), label)


class SeasonLifecycleHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, role="league_admin"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _seed_season(self, name="S1"):
        """Seed a Program + Season straight into the live server store and
        return the season id, avoiding a long HTTP build for lifecycle tests."""
        STATE.reset()
        store = STATE.api.store
        store.add_program(Program(id="p", name="P"))
        store.add_season(Season(id="s", program_id="p", name=name))
        return "s"

    def test_archive_reopen_contract(self):
        sid = self._seed_season()
        # Archive (League Admin) → 200 + archived status.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "season over"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "archived")
        # Reopen without a reason → 400 reason_required.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen", {})
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "reason_required")
        # Reopen with a reason → 200 active.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen",
                             {"reason": "fix"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "active")

    def test_archive_requires_manage_setup(self):
        sid = self._seed_season()
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "x"}, role="coach")
        self.assertEqual(st, 403, body)
        self.assertEqual(STATE.api.store.get_season(sid).status,
                         SeasonStatus.ACTIVE)  # unchanged

    def test_archive_rejects_unknown_body_key(self):
        sid = self._seed_season()
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "x", "bogus": 1})
        self.assertEqual(st, 400, body)
        self.assertEqual(STATE.api.store.get_season(sid).status,
                         SeasonStatus.ACTIVE)

    def test_archive_unknown_season_404(self):
        self._seed_season()
        st, body = self._req("POST", "/api/v2/setup/seasons/missing/archive",
                             {"reason": "x"})
        self.assertEqual(st, 404, body)

    def test_write_into_archived_season_blocked_over_http(self):
        sid = self._seed_season(name="From")
        store = STATE.api.store
        # A second, archived Season as a roll-forward target (no team setup
        # needed — the guard fires before any copy).
        store.add_season(Season(id="s2", program_id="p", name="To",
                                status=SeasonStatus.ARCHIVED))
        st, body = self._req(
            "POST", "/api/v2/setup/seasons/s2/roll-forward",
            {"from_season_id": sid})
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "season_archived")


if __name__ == "__main__":
    unittest.main()
