"""The grant-only facility contract (#369 review).

Cross-Program venue sharing needs ONE reach across the Program ceiling: an
arena serves several leagues, and the moment Program A takes a grant the Venue
is linked to A, so under the ceiling alone no other Program can ever obtain the
grant that would make it visible. The capability deadlocks on its own first use.

The first attempt satisfied that by adding `grantable_venues` to
`get_setup_overview_v2`, and it failed review for a reason worth recording
here, because it is the trap this whole module exists to keep shut:

    `/api/v2/setup/overview` is gated MANAGE_ARENA; the grant POST needs
    MANAGE_SETUP. So an Arena Manager -- a role that cannot perform the sharing
    action at all -- received every linked Venue's id and name, independent of
    its active Program. The disclosure was not bounded to the feature needing
    it, and ordinary read visibility had become de-facto write authorization.

So the candidate list moved to its own MANAGE_SETUP-gated route,
`GET /api/v2/setup/seasons/<id>/venue-candidates`, bound to a valid destination
Season inside the caller's active Program. This module pins BOTH halves:

* the ordinary overview stays ceilinged and enumerates no foreign Venue, for
  every role; and
* the candidate contract is unavailable to roles that cannot grant, and returns
  only legitimate candidates to the role that can.
"""

import http.client
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = ("admin", Role.LEAGUE_ADMIN, {})


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        store = SqlStore(url)
        store.clear_all_data()
        yield "postgres", store


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _fixture(api):
    """Two Programs with facilities, plus every link shape the contract must
    distinguish: an ACTIVE grant, a REVOKED-ONLY grant, a LEGACY Program link,
    and another operator's UNLINKED draft."""
    out = {}
    for tag in ("A", "B"):
        program = api.create_program(f"FX Prog {tag}", "US", "UTC")
        season = api.create_season(program["id"], f"FX Season {tag}")
        org = api.create_organization(f"FX Org {tag}")
        venue = api.create_venue(f"FX-VENUE-{tag}", "", "UTC", org["id"], None)
        rink = api.create_rink(venue["id"], f"FX-RINK-{tag}")
        slot = api.create_ice_slot(rink["id"], "2028-01-01T10:00:00+00:00",
                                   "2028-01-01T11:00:00+00:00", "game")
        api.grant_season_venue_access(season["id"], venue["id"])
        out[tag] = {"program": program, "season": season, "org": org,
                    "venue": venue, "rink": rink, "slot": slot}

    # REVOKED-ONLY: linked to Program B by history alone. Revoking deactivates
    # rather than deletes, so this is still an established facility.
    revoked = api.create_venue("FX-REVOKED-VENUE", "", "UTC",
                               out["B"]["org"]["id"], None)
    grant = api.grant_season_venue_access(out["B"]["season"]["id"],
                                          revoked["id"])
    api.revoke_season_venue_access(grant["id"] if isinstance(grant, dict)
                                   else grant.id)
    out["revoked"] = revoked

    # LEGACY Program link: Venue.league_id stores a PROGRAM id despite its name.
    out["legacy"] = api.create_venue("FX-LEGACY-VENUE", "", "UTC", None,
                                     out["B"]["program"]["id"])

    # Another operator's UNLINKED draft -- never a candidate for anyone else.
    out["draft"] = api.create_venue("FX-DRAFT-VENUE", "", "UTC", None, None,
                                    actor_id="user_someone_else")
    return out


class OrdinaryOverviewStaysCeilingedTest(unittest.TestCase):
    """Half one: the ordinary read discloses no foreign Venue, to ANY role."""

    def test_no_role_sees_a_foreign_venue_in_the_ordinary_overview(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                # Program B active, per the reviewer's scenario.
                exercised = []
                for label, ident in (
                        ("league_admin", ("admin", Role.LEAGUE_ADMIN, {})),
                        ("arena_manager", ("arena", Role.ARENA_MANAGER, {})),
                        ("zero_scope", ("nobody", Role.ARENA_MANAGER,
                                        {"program_id": "program_missing"}))):
                    # Context is PER USER, so it must be set for the very
                    # identity that then reads -- setting it as the admin and
                    # reading as the Arena Manager left that caller resolving
                    # to Program A and proved nothing.
                    api.set_active_context(*ident,
                                           fx["B"]["program"]["id"],
                                           fx["B"]["season"]["id"])
                    ov = api.get_setup_overview_v2(*ident)
                    if "error" in ov:
                        continue          # a denied identity discloses nothing
                    exercised.append(label)
                    blob = json.dumps(ov)
                    self.assertNotIn(
                        "grantable_venues", blob,
                        f"[{backend}/{label}] the ordinary overview grew the "
                        f"candidate list back -- that is the reviewed defect")
                    for token in ("FX-VENUE-A", fx["A"]["venue"]["id"],
                                  "FX-DRAFT-VENUE", fx["draft"]["id"]):
                        self.assertNotIn(
                            token, blob,
                            f"[{backend}/{label}] the ordinary overview "
                            f"enumerated {token!r} while Program B was active")
                # Non-vacuity: at least the two operator roles must actually
                # have produced a payload, or the `continue` above would let
                # this pass without reading anything.
                self.assertIn("league_admin", exercised, backend)
                self.assertIn(
                    "arena_manager", exercised,
                    f"[{backend}] the Arena Manager never produced an "
                    f"overview, so its ceiling was never checked")
                _close(store)


class GrantCandidateContractTest(unittest.TestCase):
    """Half two: the candidate set itself."""

    def test_candidates_are_established_facilities_and_own_drafts_only(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                res = api.get_venue_grant_candidates(fx["A"]["season"]["id"],
                                                     *ADMIN)
                self.assertNotIn("error", res, res)
                ids = {c["id"] for c in res["candidates"]}

                # IN: another Program's active-granted Venue (the feature),
                # its revoked-only Venue, and a legacy Program-linked one.
                self.assertIn(fx["B"]["venue"]["id"], ids,
                              f"[{backend}] sharing deadlocks: Program B's "
                              f"arena is not a candidate")
                self.assertIn(fx["revoked"]["id"], ids,
                              f"[{backend}] a revoked-only grant still ties "
                              f"the Venue to a Program; it stays a candidate")
                self.assertIn(fx["legacy"]["id"], ids,
                              f"[{backend}] the legacy Program link was missed")

                # OUT: another operator's unlinked draft, and the Venue this
                # Season already holds (it is not a CANDIDATE, it is granted).
                self.assertNotIn(
                    fx["draft"]["id"], ids,
                    f"[{backend}] another operator's never-linked draft is "
                    f"offerable -- the exception reached around the "
                    f"creator-only pending-link contract")
                self.assertNotIn(fx["A"]["venue"]["id"], ids, backend)

                # Candidates carry a building's id and name and nothing else.
                for c in res["candidates"]:
                    self.assertEqual(set(c), {"id", "name"}, (backend, c))

                # No Rink or IceSlot rides along.
                blob = json.dumps(res)
                self.assertNotIn(fx["B"]["rink"]["id"], blob, backend)
                self.assertNotIn(fx["B"]["slot"]["id"], blob, backend)
                _close(store)

    def test_a_foreign_or_nonexistent_season_is_refused_identically(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                foreign = api.get_venue_grant_candidates(
                    fx["B"]["season"]["id"], *ADMIN)
                missing = api.get_venue_grant_candidates(
                    "season_never_existed", *ADMIN)
                self.assertIn("error", foreign, foreign)
                self.assertIn("error", missing, missing)
                self.assertEqual(
                    foreign["error"]["code"], missing["error"]["code"],
                    f"[{backend}] an inaccessible Season is distinguishable "
                    f"from a nonexistent one: {foreign} vs {missing}")
                # ...and neither refusal names anything.
                for res in (foreign, missing):
                    for token in ("FX-VENUE-B", "FX Season B"):
                        self.assertNotIn(token, json.dumps(res),
                                         (backend, res))
                _close(store)


class GrantCandidateHttpPermissionTest(unittest.TestCase):
    """The permission boundary, which only exists at the route.

    A facade test passes `role` by hand and would agree with itself even if the
    route were gated differently -- and the reviewed defect was precisely a
    mismatch between two routes' gates, so this is the layer that matters.
    """

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
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException) as e:
            return 0, json.dumps({"transport_error": repr(e)}).encode()

    def _login(self, username, password="demo"):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": username, "password": password})
        self.assertEqual(status, 200)
        return c

    def test_the_candidate_route_is_closed_to_roles_that_cannot_grant(self):
        admin = self._login("admin")
        status, raw = self._req(admin, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, raw[:200])
        ov = json.loads(raw)
        season_id = ov["seasons"][0]["id"]

        # The grant-capable role gets the contract...
        status, raw = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("candidates", json.loads(raw))

        # ...and the role that cannot grant is refused OUTRIGHT, rather than
        # receiving the same list through a more weakly gated route.
        arena = self._login("arena")
        status, raw = self._req(
            arena, "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(
            status, 403,
            f"an Arena Manager reached the grant-candidate contract: "
            f"{raw[:300]!r}")
        self.assertIn(b"manage_setup", raw)

        # ...and its ordinary overview carries no candidate list at all.
        status, raw = self._req(arena, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200)
        self.assertNotIn(
            b"grantable_venues", raw,
            "the Arena Manager's ordinary overview still carries the "
            "candidate list -- the reviewed defect")

        # Unauthenticated is refused before anything is computed.
        status, _ = self._req(
            self._client(), "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(status, 401)

    def test_a_refused_candidate_read_mutates_nothing(self):
        admin = self._login("admin")
        store = self.srv.STATE.api.store
        grants_before = {a.id for a in store.all_season_venue_access()}
        audit_before = len(store.all_setup_audit())

        arena = self._login("arena")
        self._req(arena, "GET",
                  "/api/v2/setup/seasons/season_1/venue-candidates")
        self._req(admin, "GET",
                  "/api/v2/setup/seasons/season_never_existed/venue-candidates")

        self.assertEqual({a.id for a in store.all_season_venue_access()},
                         grants_before, "a refused read created a grant")
        self.assertEqual(len(store.all_setup_audit()), audit_before,
                         "a refused read wrote an audit row")
