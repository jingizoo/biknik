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
        # #409 -- the fixture now SELECTS. `assign-<target>` is a GUARDED
        # mutation, so it requires an EXPLICITLY persisted Program: a fallback
        # never authorizes a write, and without a choice the reassign probes
        # below stop at `active_context_required` and never reach the
        # cross-owner gate this module exists to pin. Each account simply
        # persists the tuple the resolve was already handing it, so every
        # probe is aimed at exactly the hierarchy it always was.
        ctx = self._req(c, "GET", "/api/context")[1]
        if ctx.get("program_id"):
            status, sel = self._req(c, "POST", "/api/context",
                                    {"program_id": ctx["program_id"]})
            self.assertEqual(status, 200, sel)
        return c

    def _post(self, c, entity, body, api="v2"):
        prefix = "/api/v2/setup" if api == "v2" else "/api/setup"
        status, resp = self._req(c, "POST", f"{prefix}/{entity}", body)
        self.assertEqual(status, 200, (api, entity, resp))
        self.assertNotIn("error", resp, (api, entity, resp))
        return resp

    def _operated_org(self, admin, org_name, program_name):
        """Create an Organization plus a Program it OPERATES, leaving the
        caller's persisted context exactly as it found it.

        The link step is ``assign-organization`` -- a reassign on an EXISTING
        Program -- so its SOURCE end is ceilinged on the ACTIVE Program (#369
        target authorization) and is only accepted while that Program is
        selected. The fixture therefore selects the brand-new Program for that
        one call and restores the previous selection immediately. Restoring is
        not cosmetic: these tests need the operator org to be absent from the
        ACTIVE Program's scoped lists (that is the whole gap they pin), and
        the persisted context is per-USER, so a selection left behind would
        leak into the next test in this class.
        """
        before = self._req(admin, "GET", "/api/context")[1]
        org = self._post(admin, "organization", {"name": org_name})
        program = self._post(admin, "program", {"name": program_name})
        status, sel = self._req(admin, "POST", "/api/context",
                                {"program_id": program["id"]})
        self.assertEqual(status, 200, sel)
        status, assigned = self._req(
            admin, "POST", f"/api/v2/setup/program/{program['id']}/"
            "assign-organization", {"operator_organization_id": org["id"]})
        self.assertEqual(status, 200, assigned)
        restore = {"program_id": before.get("program_id")}
        for axis in ("season_id", "league_id"):
            if before.get(axis):
                restore[axis] = before[axis]
        status, back = self._req(admin, "POST", "/api/context", restore)
        self.assertEqual(status, 200, back)
        self.assertEqual(
            self._req(admin, "GET", "/api/context")[1].get("program_id"),
            before.get("program_id"),
            "fixture drifted: the pre-existing selection was not restored, so "
            "the operator org could be in the ACTIVE Program's scoped list")
        return org, program

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

        org, _program = self._operated_org(admin, "OPERATOR-ORG",
                                           "Operated Program")

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

    def test_reassign_is_gated_like_create_on_both_api_versions(self):
        """A create is not the only way to attach a record to a parent.

        ``assign-<target>`` MOVES an existing record under a new parent, and a
        gate on creates alone leaves the identical cross-owner write open
        behind a different URL. Before this was closed, an Arena Manager could
        move its own Rink under another creator's Venue and get a 200 on both
        API versions -- the owner's Venue silently acquiring a Rink it never
        agreed to, which is the same mutation the create gate exists to stop.
        """
        _admin, parents = self._owner_parents("REASSIGN")
        arena = self._login("arena")
        for api in ("v1", "v2"):
            prefix = "/api/v2/setup" if api == "v2" else "/api/setup"
            own_venue = self._post(arena, "venue",
                                   {"name": f"ARENA-{api}-VENUE"})
            own_rink = self._post(arena, "rink",
                                  {"venue_id": own_venue["id"],
                                   "name": f"ARENA-{api}-RINK"})
            moves = [
                (f"rink/{own_rink['id']}/assign-venue", "venue_id",
                 parents["venue_id"], "Venue"),
                (f"venue/{own_venue['id']}/assign-organization",
                 "organization_id", parents["organization_id"],
                 "Organization"),
            ]
            for path, key, foreign_id, label in moves:
                status, resp = self._req(arena, "POST", f"{prefix}/{path}",
                                         {key: foreign_id})
                self.assertEqual(
                    status, 404,
                    f"[{api}] an Arena Manager moved a record under another "
                    f"creator's {key}={foreign_id}: {resp}")
                self.assertEqual(resp["error"]["message"],
                                 f"{label} {foreign_id} not found.",
                                 (api, resp))
                ghost = f"{key[:-3]}_never_existed"
                g_status, g_resp = self._req(arena, "POST",
                                             f"{prefix}/{path}", {key: ghost})
                self.assertEqual(
                    (g_status, g_resp),
                    (status, {"error": {
                        "code": resp["error"]["code"],
                        "message": f"{label} {ghost} not found."}}),
                    f"[{api}] the inaccessible-{key} reassign refusal is "
                    f"distinguishable from the nonexistent one")

            # The record did not move.
            ov = self._req(arena, "GET", "/api/v2/setup/overview")[1]
            rink = next(r for r in ov["pending_link_rinks"]
                        if r["id"] == own_rink["id"])
            self.assertEqual(rink["venue_id"], own_venue["id"],
                             f"[{api}] a refused reassign still moved the "
                             f"Rink: {rink}")

            # Positive controls: the explicit null unassign is NOT gated
            # (there is no parent to leak), and a move under a parent this
            # caller owns still works.
            status, _ = self._req(
                arena, "POST",
                f"{prefix}/venue/{own_venue['id']}/assign-organization",
                {"organization_id": None})
            self.assertEqual(status, 200,
                             f"[{api}] the explicit unassign was refused")
            own_org = self._post(arena, "organization",
                                 {"name": f"ARENA-{api}-ORG"})
            status, _ = self._req(
                arena, "POST",
                f"{prefix}/venue/{own_venue['id']}/assign-organization",
                {"organization_id": own_org["id"]})
            self.assertEqual(
                status, 200,
                f"[{api}] a move under the caller's OWN Organization was "
                f"refused -- the gate is not a scope decision but a blanket "
                f"block")

    def test_the_v1_venue_league_id_is_a_second_parent_and_is_gated_too(self):
        """`/api/setup/venue` carries TWO parent ids, and gating one of them
        left the identical attachment reachable one field over.

        `league_id` is the legacy v1-only Venue->Program link (that field
        stores a PROGRAM id despite its name). `create_venue` resolves the
        Program's operator Organization and OVERWRITES `organization_id` with
        it -- so a caller refused at `organization_id: org_N` could pass
        `league_id: <the Program org_N operates>` and land a Venue carrying
        org_N anyway, then keep building on it (the poached Venue is now
        creator-owned, so the gate itself admits the follow-on Rink). The
        200-vs-404 split was an existence oracle over Program ids on top.

        Found by adversarial audit AFTER the create gate shipped and passed
        CI, which is why the negative below asserts the two refusals are
        identical rather than merely both non-200.
        """
        admin = self._login("admin")
        org, program = self._operated_org(admin, "LEAGUEID-ORG",
                                          "LEAGUEID-PROGRAM")

        arena = self._login("arena")
        ov = self._req(arena, "GET", "/api/v2/setup/overview")[1]
        readable = ({r["id"] for r in ov["organizations"]}
                    | {r["id"] for r in ov["pending_link_organizations"]})
        self.assertNotIn(org["id"], readable,
                         "fixture drifted: the victim org is readable, so "
                         "this proves nothing")

        # The gated field refuses...
        gated_status, gated = self._req(
            arena, "POST", "/api/setup/venue",
            {"name": "control", "organization_id": org["id"]})
        self.assertEqual(gated_status, 404, gated)

        # ...and so must the second parent on the very same route.
        status, resp = self._req(arena, "POST", "/api/setup/venue",
                                 {"name": "poached",
                                  "league_id": program["id"]})
        self.assertEqual(
            status, 404,
            f"an Arena Manager created a Venue inside a Program it cannot "
            f"read, via league_id: {resp}")
        self.assertEqual(resp["error"]["message"],
                         f"Program {program['id']} not found.", resp)
        self.assertNotIn(
            "poached", json.dumps([v.__dict__ for v in
                                   self.srv.STATE.api.store.all_venues()]),
            "the refused Venue was written anyway")

        # No existence oracle: an inaccessible Program and a nonexistent one
        # are indistinguishable.
        ghost_status, ghost = self._req(
            arena, "POST", "/api/setup/venue",
            {"name": "poached", "league_id": "league_never_existed"})
        self.assertEqual(
            (ghost_status, ghost),
            (status, {"error": {
                "code": resp["error"]["code"],
                "message": "Program league_never_existed not found."}}),
            f"200/404 on league_id discloses which Programs exist: {resp} vs "
            f"{ghost}")

        # Positive control: the creator's OWN Program still accepts it, so
        # the legacy v1 link is gated rather than disabled. #409: the v1
        # `league_id` IS the parent Program, so the operator must be standing
        # in it -- select it, which is what "the creator's OWN Program" means.
        status, sel = self._req(admin, "POST", "/api/context",
                                {"program_id": program["id"]})
        self.assertEqual(status, 200, sel)
        self._post(admin, "venue", {"name": "own-legacy-link",
                                    "league_id": program["id"]}, api="v1")

    def test_every_declared_reassign_parent_has_a_resolvable_list(self):
        """Every RouteSpec parent kind must be executable by the live gate.

        Completeness and exact values are independently pinned by
        ``test_route_registry`` over production's selector. This test owns the
        cross-module invariant: every selected kind must name a parent list
        the request-time refusal can actually resolve.
        """
        from hockey_scheduler.web.route_registry import (
            REASSIGNMENT_PARENT_SPECS)
        from hockey_scheduler.web.server import _SETUP_PARENT_LISTS
        for spec in REASSIGNMENT_PARENT_SPECS:
            with self.subTest(spec=spec.name):
                self.assertIn(spec.reassignment_parent_kind,
                              _SETUP_PARENT_LISTS)

    def test_a_second_account_cannot_reach_that_same_operator_org(self):
        """The other half of the case above: creator-ownership admits the
        creator and nobody else. Without this, "allow rows you created" could
        have been implemented as "allow linked rows" and still looked green.
        """
        admin = self._login("admin")
        org, _program = self._operated_org(admin, "OPERATOR-ORG-2",
                                           "Operated Program 2")

        arena = self._login("arena")
        self._assert_refused_like_nonexistent(
            arena, "v2", "venue", "organization_id", org["id"],
            "Organization", {"name": "poached-venue"},
            "an Arena Manager")
