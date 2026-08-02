"""Official availability (#88).

Officials declare available/unavailable windows. Assigning an official to a
game that overlaps an unavailable window is blocked, but an operator can
override with a warning. An official manages only their own availability; an
operator can view/manage all.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import OfficialAvailabilityStatus
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import InMemoryStore, SqlStore


class AvailabilityServiceContract:
    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store(self._store())
        self.api = ApiService(self.store)
        self.ref = self.ids["referee_id"]
        self.game = self.store.get_game(self.game_id)

    def _mark_unavailable(self):
        st = self.game.start_time
        return self.api.set_official_availability(
            self.ref, st.isoformat(), (st + timedelta(hours=2)).isoformat(),
            "unavailable", note="Away")

    def test_set_and_list_availability(self):
        self._mark_unavailable()
        got = self.api.list_official_availability(self.ref)["availability"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], "unavailable")
        self.assertEqual(got[0]["note"], "Away")

    def test_end_before_start_rejected(self):
        st = self.game.start_time
        res = self.api.set_official_availability(
            self.ref, st.isoformat(), (st - timedelta(hours=1)).isoformat(),
            "unavailable")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_assign_blocked_when_unavailable(self):
        # Free the referee first (seed assigns them), then mark unavailable.
        for a in self.store.assignments_for_game(self.game_id):
            if a.official_id == self.ref:
                self.api.unassign_official(a.id)
        self._mark_unavailable()
        res = self.api.assign_official(self.game_id, self.ref, "referee")
        self.assertEqual(res["error"]["details"]["reason"], "official_unavailable")

    def test_operator_override_assigns_anyway(self):
        for a in self.store.assignments_for_game(self.game_id):
            if a.official_id == self.ref:
                self.api.unassign_official(a.id)
        self._mark_unavailable()
        res = self.api.assign_official(self.game_id, self.ref, "referee",
                                       override_unavailable=True)
        self.assertNotIn("error", res)
        self.assertEqual(res["official_id"], self.ref)

    def test_available_window_does_not_block(self):
        for a in self.store.assignments_for_game(self.game_id):
            if a.official_id == self.ref:
                self.api.unassign_official(a.id)
        st = self.game.start_time
        self.api.set_official_availability(
            self.ref, st.isoformat(), (st + timedelta(hours=2)).isoformat(),
            "available")
        res = self.api.assign_official(self.game_id, self.ref, "referee")
        self.assertNotIn("error", res)

    def test_accepted_assignment_still_works_after_marking_unavailable(self):
        # A prior accepted assignment is untouched by later availability edits.
        assignment = self.store.assignments_for_game(self.game_id)[0]
        self.api.respond_assignment(assignment.id, accept=True)
        self._mark_unavailable()
        again = self.store.get_official_assignment(assignment.id)
        self.assertEqual(again.status.value, "accepted")


class MemoryAvailabilityTest(AvailabilityServiceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlAvailabilityTest(AvailabilityServiceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class TransactionBoundaryTest(unittest.TestCase):
    """The @_transactional decorator sits on the right method (#88).

    The bug: the decorator had drifted off ``assign_official`` (which mutates +
    audits + notifies and must be atomic) onto ``set_official_availability``.
    These count how many transactions each call opens on a live SqlStore, which
    fails before the fix (assign=0, set_availability=1) and passes after
    (assign=1, set_availability=0). InMemoryStore's no-op transaction can't
    distinguish these, so SqlStore is used.
    """

    def _api_counting(self):
        store, gid, ids = build_full_demo_store(SqlStore(":memory:"))
        api = ApiService(store)
        calls = {"n": 0}
        real = store.transaction

        def counting():
            calls["n"] += 1
            return real()

        store.transaction = counting  # patch AFTER the demo build
        return api, store, gid, ids, calls

    def test_assign_official_opens_exactly_one_transaction(self):
        api, store, gid, ids, calls = self._api_counting()
        ref = ids["referee_id"]
        for a in store.assignments_for_game(gid):  # free the seeded referee
            if a.official_id == ref:
                api.unassign_official(a.id)
        calls["n"] = 0  # reset after the unassign setup
        res = api.assign_official(gid, ref, "referee")
        self.assertNotIn("error", res)
        self.assertEqual(calls["n"], 1)  # was 0 while the decorator was orphaned

    def test_set_official_availability_opens_no_transaction(self):
        api, store, gid, ids, calls = self._api_counting()
        ref = ids["referee_id"]
        st = store.get_game(gid).start_time
        res = api.set_official_availability(
            ref, st.isoformat(), (st + timedelta(hours=2)).isoformat(),
            "available")
        self.assertNotIn("error", res)
        self.assertEqual(calls["n"], 0)  # decorator no longer accidentally here

    def test_unassign_official_opens_exactly_one_transaction(self):
        api, store, gid, ids, calls = self._api_counting()
        ref = ids["referee_id"]
        assignment = next(a for a in store.assignments_for_game(gid)
                          if a.official_id == ref)
        calls["n"] = 0
        api.unassign_official(assignment.id)
        self.assertEqual(calls["n"], 1)


class AvailabilityHttpAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.ref = srv.STATE.ids["referee_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_official_manages_own_availability(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "official", "password": "demo"})
        status, body = self._req(
            c, "POST", f"/api/officials/{self.ref}/availability",
            {"start_time": "2027-01-01T18:00:00Z", "end_time": "2027-01-01T20:00:00Z",
             "status": "unavailable"})
        self.assertEqual(status, 200)
        status, listed = self._req(c, "GET", f"/api/officials/{self.ref}/availability")
        self.assertEqual(status, 200)
        self.assertTrue(listed["availability"])

    def test_official_cannot_edit_another_official(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "official", "password": "demo"})
        status, _ = self._req(
            c, "POST", "/api/officials/official_other/availability",
            {"start_time": "2027-01-01T18:00:00Z", "end_time": "2027-01-01T20:00:00Z",
             "status": "unavailable"})
        self.assertEqual(status, 403)
        status2, _ = self._req(c, "GET", "/api/officials/official_other/availability")
        self.assertEqual(status2, 403)

    def test_operator_can_view_any(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, _ = self._req(c, "GET", f"/api/officials/{self.ref}/availability")
        self.assertEqual(status, 200)

    def _last_audit(self, action):
        entries = [a for a in self.srv.STATE.api.store.all_setup_audit()
                   if a.action == action]
        return entries[-1] if entries else None

    def test_set_availability_attributes_signed_in_official_not_body(self):
        # An official creating their own window is audited against their
        # server-resolved account, never a forged body actor_id (#88). The route
        # must not trust ``actor_id`` from the request body.
        official_uid = self.srv.STATE.api.verify_login("official", "demo")["id"]
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "official", "password": "demo"})
        status, created = self._req(
            c, "POST", f"/api/officials/{self.ref}/availability",
            {"start_time": "2027-02-01T18:00:00Z",
             "end_time": "2027-02-01T20:00:00Z",
             "status": "unavailable", "actor_id": "attacker"})
        self.assertEqual(status, 200)
        entry = self._last_audit("official_availability_set")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.entity_id, created["id"])
        self.assertEqual(entry.actor_id, official_uid)
        self.assertNotEqual(entry.actor_id, "attacker")

    def test_delete_availability_attributes_signed_in_operator_not_body(self):
        # An operator deleting a window is audited against the operator's
        # resolved account, never a forged body actor_id (#88).
        admin_uid = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        _, created = self._req(
            c, "POST", f"/api/officials/{self.ref}/availability",
            {"start_time": "2027-03-01T18:00:00Z",
             "end_time": "2027-03-01T20:00:00Z", "status": "available"})
        avail_id = created["id"]
        status, _ = self._req(
            c, "POST", f"/api/officials/availability/{avail_id}/delete",
            {"actor_id": "attacker"})
        self.assertEqual(status, 200)
        entry = self._last_audit("official_availability_deleted")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.entity_id, avail_id)
        self.assertEqual(entry.actor_id, admin_uid)
        self.assertNotEqual(entry.actor_id, "attacker")


if __name__ == "__main__":
    unittest.main()
