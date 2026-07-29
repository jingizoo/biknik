"""The cross-owner setup-WRITE gate (#369 review).

Four setup creates take a parent id straight from the request body --
rink->venue, ice-slot->rink, venue->organization, official->home club. Before
this round every one of them wrote wherever it was pointed. Redacting the
resolved parent NAME out of the read closed the read-side oracle and left the
write itself untouched, which is the finding this module answers: an operator
authorized for zero of the relevant Programs could still POST a Rink under
another creator's guessed ``venue_N``, ice under a foreign ``rink_N``, a Venue
under a foreign ``org_N``, or an Official -- carrying a person's name -- homed
at a foreign ``club_N``.

``test_pending_link_ownership.py`` asserts the refusal on the canonical v2
routes. This module covers the three things that file does not:

* **v1 is not a bypass.** The same four creates exist under ``/api/setup/``
  and are still live. A gate applied only to v2 would be no gate at all --
  every probe simply moves one URL over.
* **The creator-owned source is REQUIRED, not a convenience.** "Create an
  Organization, create a Program it operates, then add that Organization's
  first Venue" is an ordinary setup flow, and it is invisible to both of the
  read's lists at the moment the Venue is created: operating a Program is a
  real Program link (so the row is correctly not ``pending_link_*``), while
  the caller's context still points at another Program (so it is correctly
  not in the scoped list). A gate built from those two lists alone refuses
  it. The regression that caught this was 20 backend failures, so it is
  pinned here directly rather than left to be re-discovered.
* **Ownership is narrower than authorization.** A League Admin is a
  ``_GLOBAL_ROLE`` -- authorized for every Program in the installation. That
  is precisely why the ruling rejected the authorized-set alternative, and
  why the gate must not quietly become it: a global role has still CREATED
  only its own rows, and must still be refused at another creator's parent.

Every refusal is asserted to be indistinguishable from a parent id that never
existed, since "accepted vs not found" over sequential ids is itself the
disclosure.
"""

import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

# (route, body key naming the parent, the noun the facade uses for a parent
# of that kind that genuinely does not exist, the rest of a valid body)
_SLOT_TIMES = {"start_time": "2028-03-01T10:00:00+00:00",
               "end_time": "2028-03-01T11:00:00+00:00"}
PARENT_CREATES = [
    ("rink", "venue_id", "Venue", {"name": "probe-rink"}),
    ("ice-slot", "rink_id", "Rink", _SLOT_TIMES),
    ("venue", "organization_id", "Organization", {"name": "probe-venue"}),
    ("official", "home_club_id", "Club", {"name": "probe-official"}),
]


class SetupParentWriteScopeHttpTest(unittest.TestCase):
    """Real sessions, real routes -- the only layer where "the id in the body"
    and "the identity that sent it" are genuinely independent."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

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
        except (urllib.error.URLError, http.client.HTTPException) as e:
            # A transport failure is a RESULT, not a harness crash -- report
            # it as a synthetic status so the assertion being made produces
            # its own message rather than an opaque traceback.
            return 0, {"transport_error": repr(e)}

    def _login(self, username, password="demo"):
        c = self._client()
        status, resp = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": password})
        self.assertEqual(status, 200, resp)
        return c

    def _post(self, c, entity, body, api="v2"):
        prefix = "/api/v2/setup" if api == "v2" else "/api/setup"
        status, resp = self._req(c, "POST", f"{prefix}/{entity}", body)
        self.assertEqual(status, 200, (api, entity, resp))
        self.assertNotIn("error", resp, (api, entity, resp))
        return resp

    def _owner_parents(self, tag):
        """A full set of parents owned by the seeded admin, one per relation."""
        admin = self._login("admin")
        venue = self._post(admin, "venue", {"name": f"{tag}-VENUE"})
        rink = self._post(admin, "rink", {"venue_id": venue["id"],
                                          "name": f"{tag}-RINK"})
        org = self._post(admin, "organization", {"name": f"{tag}-ORG"})
        club = self._post(admin, "club", {"name": f"{tag}-CLUB"})
        return admin, {"venue_id": venue["id"], "rink_id": rink["id"],
                       "organization_id": org["id"], "home_club_id": club["id"]}

    def _assert_refused_like_nonexistent(self, c, api, entity, key, parent_id,
                                         label, rest, who):
        prefix = "/api/v2/setup" if api == "v2" else "/api/setup"
        status, resp = self._req(c, "POST", f"{prefix}/{entity}",
                                 {key: parent_id, **rest})
        self.assertEqual(
            status, 404,
            f"[{api}] {who} wrote a {entity} under another creator's "
            f"{key}={parent_id}: {resp}")
        self.assertEqual(resp["error"]["message"],
                         f"{label} {parent_id} not found.", (api, resp))
        ghost_id = f"{key[:-3]}_never_existed"
        ghost_status, ghost_resp = self._req(c, "POST", f"{prefix}/{entity}",
                                             {key: ghost_id, **rest})
        self.assertEqual(
            (ghost_status, ghost_resp),
            (status, {"error": {"code": resp["error"]["code"],
                                "message": f"{label} {ghost_id} not found."}}),
            f"[{api}] the inaccessible-{key} refusal is distinguishable from "
            f"the nonexistent one: {resp} vs {ghost_resp}")

    def test_the_v1_routes_are_not_a_bypass(self):
        """The identical four probes against ``/api/setup/`` -- the older,
        still-live write surface. If the gate lived only on v2 the whole
        finding would reopen by changing one path segment."""
        _admin, parents = self._owner_parents("V1PROBE")
        arena = self._login("arena")
        for entity, key, label, rest in PARENT_CREATES:
            self._assert_refused_like_nonexistent(
                arena, "v1", entity, key, parents[key], label, rest,
                "an Arena Manager")

    def test_a_global_role_is_still_refused_at_another_creators_parent(self):
        """Authorization is not ownership. A second League Admin is authorized
        for every Program in the installation -- the exact identity the
        ruling's rejected authorized-set alternative would have waved
        through -- and is still refused, on both API versions."""
        admin, parents = self._owner_parents("GLOBALPROBE")
        status, acct = self._req(admin, "POST", "/api/accounts",
                                 {"username": "second_admin",
                                  "password": "demo2",
                                  "role": "league_admin", "scope": {}})
        self.assertEqual(status, 200, acct)
        other = self._login("second_admin", "demo2")
        for api in ("v1", "v2"):
            for entity, key, label, rest in PARENT_CREATES:
                self._assert_refused_like_nonexistent(
                    other, api, entity, key, parents[key], label, rest,
                    "a second League Admin")

    def test_a_creator_can_add_a_venue_to_an_org_that_operates_a_program(self):
        """The flow the gate must NOT refuse, and the one a scoped-plus-pending
        gate does refuse.

        The Organization is linked (it operates a Program) so it is correctly
        absent from ``pending_link_organizations``; the caller's active
        context is a DIFFERENT Program, so it is correctly absent from the
        scoped ``organizations`` too. Only creator-ownership carries it. The
        create-then-link chain below then continues through Rink and IceSlot,
        because a gate that admitted the Organization but lost the Venue it
        just produced would be no more usable.
        """
        admin = self._login("admin")
        # Something else must be active, or the new Program would simply
        # become the active one and the scoped list would cover the org --
        # which would make this test pass without exercising the gap at all.
        status, ov = self._req(admin, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, ov)
        self.assertTrue(ov["programs"],
                        "fixture needs a pre-existing active Program for the "
                        "new org's Program to NOT be the active one")

        org = self._post(admin, "organization", {"name": "OPERATOR-ORG"})
        program = self._post(admin, "program", {"name": "Operated Program"})
        status, assigned = self._req(
            admin, "POST", f"/api/v2/setup/program/{program['id']}/"
            "assign-organization", {"operator_organization_id": org["id"]})
        self.assertEqual(status, 200, assigned)

        ov = self._req(admin, "GET", "/api/v2/setup/overview")[1]
        self.assertNotIn(
            org["id"], {r["id"] for r in ov["pending_link_organizations"]},
            "fixture drifted: the operator org is still pending-link, so this "
            "test would pass without needing the creator-owned source")
        self.assertNotIn(
            org["id"], {r["id"] for r in ov["organizations"]},
            "fixture drifted: the operator org is in the active scoped list, "
            "so this test would pass without needing the creator-owned source")

        venue = self._post(admin, "venue", {"name": "OPERATOR-VENUE",
                                            "organization_id": org["id"]})
        rink = self._post(admin, "rink", {"venue_id": venue["id"],
                                          "name": "OPERATOR-RINK"})
        self._post(admin, "ice-slot", dict(rink_id=rink["id"], **_SLOT_TIMES))

    def test_a_second_account_cannot_reach_that_same_operator_org(self):
        """The other half of the case above: creator-ownership admits the
        creator and nobody else. Without this, "allow rows you created" could
        have been implemented as "allow linked rows" and still looked green.
        """
        admin = self._login("admin")
        org = self._post(admin, "organization", {"name": "OPERATOR-ORG-2"})
        program = self._post(admin, "program", {"name": "Operated Program 2"})
        status, assigned = self._req(
            admin, "POST", f"/api/v2/setup/program/{program['id']}/"
            "assign-organization", {"operator_organization_id": org["id"]})
        self.assertEqual(status, 200, assigned)

        arena = self._login("arena")
        self._assert_refused_like_nonexistent(
            arena, "v2", "venue", "organization_id", org["id"],
            "Organization", {"name": "poached-venue"},
            "an Arena Manager")
