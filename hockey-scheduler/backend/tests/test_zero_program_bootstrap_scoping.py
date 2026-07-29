"""The zero-Program installation is scoped like every other install (#369
review blocker).

``get_setup_overview_v2`` used to carry a bootstrap carve-out: when a scoped
caller resolved no active Program AND the store held zero Programs, ``scoped``
was flipped back to ``False`` and the read fell through to the fully
unfiltered legacy shape — every Club, Organization, Venue, Rink, IceSlot and
Official in the installation, regardless of who created them. The stated
reasoning was that a brand-new install "has no other Program to leak from".

That reasoning does not survive contact with a real install. A Program-less
installation is exactly where pre-Program setup rows accumulate, Officials'
personal names among them, and two setup-capable accounts working on the same
fresh install could each enumerate everything the other had entered. An
account holding the read permission and authorized for nothing could
enumerate all of it. Absence of a Program is not an authorization: it is
neither the owner-approved creator-owned contract nor a separately authorized
install-admin boundary, so the carve-out is REVERSED here.

The correction is the narrow one — the fall-through is simply gone, and no
install-bootstrap authorization replaces it. An authenticated zero-Program
caller now gets the same answer a scope-denied caller gets: the scoped-empty
shape plus its OWN ``pending_link_*`` rows. That is sufficient for the flow
the carve-out existed to protect, and this module proves it: the operator
bootstrapping a brand-new install still sees, and still links, every record it
created (``test_the_creator_can_still_bootstrap_a_brand_new_install``).

What is asserted, on an install with LITERALLY zero Programs:

* two real, setup-capable accounts each create a distinguishable unlinked row
  of every pending-link kind; each session sees only its own — in
  ``pending_link_*``, in the scoped lists, and nowhere in the serialized
  payload at all (a name sweep, so a leak through a resolved parent-name field
  counts as a leak);
* every scoped collection is EMPTY for both, which is the direct
  anti-fall-through assertion: under the old carve-out ``clubs`` /
  ``organizations`` / ``venues`` / ``rinks`` / ``ice_slots`` / ``officials``
  each held BOTH accounts' rows;
* ``user_id=None`` with a real role sees nothing whatsoever;
* the identity-less legacy call (``role=None``, no arguments) is still fully
  unfiltered — many internal callers and tests depend on that exact shape, and
  the blocker is about AUTHENTICATED callers.

Facade-level across Memory/SQLite/(optional PostgreSQL when
``TEST_DATABASE_URL`` is set), plus an authenticated-HTTP class where the
write's ``actor_id`` and the read's ``user_id`` are both the server-resolved
session account rather than two values a facade test hands itself.

Sibling coverage, deliberately not duplicated here:
``test_pending_link_ownership.py`` is the same ownership matrix on an install
where other Programs DO exist (denial, not bootstrap);
``test_league_filtered_overview_v2.py`` owns the active-tuple scoping itself.
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

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore

# Every collection that used to be published wholesale by the carve-out.
_SCOPED_KEYS = (
    "programs", "seasons", "leagues", "divisions", "teams", "clubs",
    "organizations", "officials", "venues", "rinks", "ice_slots",
)
_PENDING_KEYS = (
    "pending_link_clubs", "pending_link_organizations",
    "pending_link_officials", "pending_link_venues", "pending_link_rinks",
    "pending_link_ice_slots",
)
_PENDING_TO_SCOPED = {
    "pending_link_clubs": "clubs",
    "pending_link_organizations": "organizations",
    "pending_link_officials": "officials",
    "pending_link_venues": "venues",
    "pending_link_rinks": "rinks",
    "pending_link_ice_slots": "ice_slots",
}


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


class ZeroProgramBootstrapScopingTest(unittest.TestCase):
    """Facade-level, across the full store matrix."""

    @staticmethod
    def _ids(ov, key):
        return {row["id"] for row in ov.get(key, [])}

    def _assert_zero_programs(self, api, label):
        """The fixture guard. Every assertion in this module is about the
        bootstrap branch specifically, so a fixture that quietly grew a
        Program would make the whole file pass for the wrong reason."""
        self.assertEqual(
            api.store.all_programs(), [],
            f"[{label}] the fixture is not a zero-Program installation, so it "
            "does not exercise the bootstrap branch at all")

    def _unlinked_set(self, api, actor_id, tag):
        """One never-linked record of every pending-link kind, created by
        ``actor_id`` and carrying ``tag`` in every name so a leak shows up as a
        NAME in the payload, not only as an id mismatch."""
        club = api.create_club(f"{tag}-Club", actor_id=actor_id)
        org = api.create_organization(f"{tag}-Org", actor_id=actor_id)
        venue = api.create_venue(f"{tag}-Venue", organization_id=org["id"],
                                 actor_id=actor_id)
        rink = api.create_rink(venue["id"], f"{tag}-Rink", actor_id=actor_id)
        slot = api.create_ice_slot(rink["id"], "2027-05-04T18:00:00+00:00",
                                   "2027-05-04T19:30:00+00:00",
                                   actor_id=actor_id)
        official = api.create_official(f"{tag}-Official",
                                       home_club_id=club["id"],
                                       actor_id=actor_id)
        for r in (club, org, venue, rink, slot, official):
            self.assertNotIn("error", r, r)
        return {"pending_link_clubs": club["id"],
                "pending_link_organizations": org["id"],
                "pending_link_venues": venue["id"],
                "pending_link_rinks": rink["id"],
                "pending_link_ice_slots": slot["id"],
                "pending_link_officials": official["id"]}

    def _two_operators(self, api):
        """Two REAL setup-capable accounts on a Program-less install, each
        with its own distinguishable unlinked inventory. Real accounts (not
        bare id strings) because the contract keys on an account id that a
        session can actually present."""
        alpha = api.create_user_account("zpb_alpha", "correct-horse-a1",
                                        "league_admin")
        self.assertNotIn("error", alpha, alpha)
        bravo = api.create_user_account("zpb_bravo", "correct-horse-b1",
                                        "arena_manager")
        self.assertNotIn("error", bravo, bravo)
        return (alpha["id"], self._unlinked_set(api, alpha["id"], "ALPHA"),
                bravo["id"], self._unlinked_set(api, bravo["id"], "BRAVO"))

    def _overview(self, api, user_id, role):
        ov = api.get_setup_overview_v2(user_id, role, {})
        self.assertNotIn("error", ov, ov)
        return ov

    # -- the blocker's required regression ----------------------------------
    def test_two_operators_on_a_zero_program_install_see_only_their_own(self):
        """The reviewer's verbatim regression: zero Programs, two real
        setup-capable accounts, distinguishable unlinked rows under each. Each
        session sees only its own, and ``user_id=None`` sees none."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                alpha, mine, bravo, theirs = self._two_operators(api)
                self._assert_zero_programs(api, label)

                for reader, role, own, foreign, foreign_tag in (
                        (alpha, Role.LEAGUE_ADMIN, mine, theirs, "BRAVO"),
                        (bravo, Role.ARENA_MANAGER, theirs, mine, "ALPHA")):
                    ov = self._overview(api, reader, role)

                    # (a0) The sharpest edge of the blocker, asserted first and
                    #      on its own so a regression REPORTS it in those
                    #      words: an Official row is a person's name.
                    self.assertNotIn(
                        foreign["pending_link_officials"],
                        self._ids(ov, "officials"),
                        f"[{label}] {reader} was handed the personal name of "
                        "an Official another account entered, because the "
                        "installation happens to have no Program yet")

                    # (a) The scoped lists are EMPTY. This is the assertion the
                    #     removed carve-out fails: it published every Club,
                    #     Organization, Venue, Rink, IceSlot and Official in
                    #     the installation here, both operators' alike.
                    for key in _SCOPED_KEYS:
                        self.assertEqual(
                            ov[key], [],
                            f"[{label}] {reader} received a non-empty `{key}` "
                            "on a zero-Program installation -- the read fell "
                            "through to the unfiltered installation-wide "
                            "shape instead of staying scoped, which is the "
                            "#369 blocker: a second setup-capable account can "
                            "enumerate every pre-Program record, Officials' "
                            "personal names included")

                    # (b) Its own creations are still there -- create-then-link
                    #     is exactly what the pending list exists for.
                    for key in _PENDING_KEYS:
                        self.assertIn(
                            own[key], self._ids(ov, key),
                            f"[{label}] {reader} lost sight of the record it "
                            f"created in {key}; a brand-new install cannot be "
                            "bootstrapped at all if the operator cannot see "
                            "its own rows")

                    # (c) The other operator's rows are in NEITHER list.
                    for key in _PENDING_KEYS:
                        self.assertNotIn(
                            foreign[key], self._ids(ov, key),
                            f"[{label}] {reader} enumerated a record it did "
                            f"not create through {key}")
                        self.assertNotIn(
                            foreign[key],
                            self._ids(ov, _PENDING_TO_SCOPED[key]),
                            f"[{label}] {reader} received a foreign "
                            f"never-linked record through "
                            f"{_PENDING_TO_SCOPED[key]}")

                    # (d) Catch-all over the SERIALIZED payload: not one
                    #     character of the other operator's naming may appear
                    #     anywhere, including inside a resolved parent-name
                    #     field (`organization_name`, `venue_name`,
                    #     `rink_name`, `home_club_name`) that no id-based
                    #     check above would notice.
                    blob = json.dumps(ov)
                    self.assertNotIn(
                        foreign_tag, blob,
                        f"[{label}] the token {foreign_tag!r} appears "
                        f"somewhere in {reader}'s payload -- a foreign "
                        "pre-Program record leaked, by name")

                # (e) An identity-less caller that still passes a role owns
                #     nothing and validates against nothing: every list empty.
                anon = self._overview(api, None, Role.LEAGUE_ADMIN)
                for key in _SCOPED_KEYS + _PENDING_KEYS:
                    self.assertEqual(
                        anon[key], [],
                        f"[{label}] user_id=None received {key} on a "
                        "zero-Program installation")
                blob = json.dumps(anon)
                for tag in ("ALPHA", "BRAVO"):
                    self.assertNotIn(tag, blob, (label, tag, "user_id=None"))
                _close(store)

    def test_the_identity_less_legacy_call_stays_fully_unfiltered(self):
        """``role=None`` (the no-argument default) is NOT what the blocker is
        about, and must not change: several internal call sites and tests read
        the full installation view through it. Asserted on the same
        zero-Program fixture so the contrast with the scoped calls above is
        exact -- same store, same rows, different contract."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                _, mine, _, theirs = self._two_operators(api)
                self._assert_zero_programs(api, label)

                ov = api.get_setup_overview_v2()
                self.assertNotIn("error", ov, ov)
                for key in _PENDING_KEYS:
                    scoped_key = _PENDING_TO_SCOPED[key]
                    for owner_label, owned in (("alpha", mine),
                                               ("bravo", theirs)):
                        self.assertIn(
                            owned[key], self._ids(ov, scoped_key),
                            f"[{label}] the unfiltered legacy shape dropped "
                            f"{owner_label}'s row from {scoped_key}")
                    # Nothing is "pending" in a shape that is already whole.
                    self.assertEqual(ov[key], [], (label, key))
                _close(store)

    def test_the_creator_can_still_bootstrap_a_brand_new_install(self):
        """The flow the carve-out was defending, proven to work without it.

        The operator on a Program-less install creates its Club/Venue and so
        on, then builds the first Program around them. Its own rows are
        reachable at every step -- pending while unlinked, scoped the moment a
        real link exists -- so nothing about the correction deadlocks a new
        installation. The second operator's still-unlinked rows stay invisible
        throughout, including AFTER a Program exists and both accounts resolve
        it."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                alpha, mine, bravo, theirs = self._two_operators(api)
                self._assert_zero_programs(api, label)

                before = self._overview(api, alpha, Role.LEAGUE_ADMIN)
                self.assertIn(mine["pending_link_clubs"],
                              self._ids(before, "pending_link_clubs"),
                              (label, "unlinked pre-check"))

                # The first Program, and the chain that links the Club it was
                # created alongside: Program -> Season -> League -> Division ->
                # Team(club=the pending Club).
                program = api.create_program("First Program", "US", "UTC",
                                             actor_id=alpha)
                self.assertNotIn("error", program, program)
                season = api.create_season(program["id"], "First Season",
                                           actor_id=alpha)
                self.assertNotIn("error", season, season)
                league = api.create_league(season["id"], "First League",
                                           actor_id=alpha)
                self.assertNotIn("error", league, league)
                division = api.create_division(season["id"], "First Division",
                                               league_id=league["id"],
                                               actor_id=alpha)
                self.assertNotIn("error", division, division)
                team = api.create_team(club_id=mine["pending_link_clubs"],
                                       division_id=division["id"],
                                       name="First Team",
                                       program_id=program["id"],
                                       league_id=league["id"], actor_id=alpha)
                self.assertNotIn("error", team, team)
                grant = api.grant_season_venue_access(
                    season["id"], mine["pending_link_venues"], actor_id=alpha)
                self.assertNotIn("error", grant, grant)

                # The creator's context now resolves the one Program it just
                # built (no explicit selection needed), and its records have
                # moved from "pending" to genuinely in scope.
                after = self._overview(api, alpha, Role.LEAGUE_ADMIN)
                self.assertEqual(self._ids(after, "programs"),
                                 {program["id"]}, (label, after["programs"]))
                for key, scoped_key in (("pending_link_clubs", "clubs"),
                                        ("pending_link_venues", "venues"),
                                        ("pending_link_rinks", "rinks")):
                    self.assertIn(
                        mine[key], self._ids(after, scoped_key),
                        f"[{label}] a record linked into the install's FIRST "
                        f"Program never arrived in {scoped_key} -- the "
                        "bootstrap flow is broken")
                    self.assertNotIn(
                        mine[key], self._ids(after, key),
                        f"[{label}] a now-linked record is still being "
                        f"offered through {key}")

                # ...and the second operator's untouched rows are still its
                # own alone, from both sides.
                self.assertNotIn(
                    "BRAVO", json.dumps(after),
                    f"[{label}] creating the first Program handed the OTHER "
                    "operator's pre-Program records to the creator")
                ov_bravo = self._overview(api, bravo, Role.ARENA_MANAGER)
                for key in _PENDING_KEYS:
                    self.assertIn(theirs[key], self._ids(ov_bravo, key),
                                  (label, key, "bravo keeps its own"))
                    self.assertNotIn(
                        mine[key], self._ids(ov_bravo, key),
                        f"[{label}] the second operator picked up the "
                        f"creator's {key} row once a Program existed")
                _close(store)


class ZeroProgramBootstrapScopingHttpTest(unittest.TestCase):
    """The same regression over real authenticated sessions.

    A facade test supplies the write's ``actor_id`` and the read's ``user_id``
    itself and would agree with itself even if production wired two different
    values. Here both come from the server-resolved session, and the install is
    a genuine cold-boot clean slate (``STATE.reset(seed=False)`` — the exact
    state the demo server boots into, and the state the browser journeys
    exercise).
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        # seed=False is the whole point: a clean slate has ZERO Programs.
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        # STATE is a module-level singleton shared with every other HTTP test
        # module; hand it back seeded, the way they expect to find it.
        cls.srv.STATE.reset()

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
            # A transport failure is a RESULT, not a harness crash — report it
            # as a synthetic status so the assertion being made produces its
            # own message instead of an opaque traceback.
            return 0, {"transport_error": repr(e)}

    def _login(self, username, password="demo"):
        c = self._client()
        status, resp = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": password})
        self.assertEqual(status, 200, resp)
        return c

    def _post(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    def _overview(self, c):
        status, ov = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, ov)
        self.assertNotIn("error", ov, ov)
        return ov

    @staticmethod
    def _ids(ov, key):
        return {row["id"] for row in ov.get(key, [])}

    def _arena_inventory(self, c, tag):
        """The rows an ARENA MANAGER can create — the exact identity the
        blocker names ("a second Arena Manager"), holding MANAGE_ARENA (+
        MANAGE_SCHEDULE for Officials) and nothing more."""
        org = self._post(c, "organization", {"name": f"{tag}-Org"})
        venue = self._post(c, "venue", {"name": f"{tag}-Venue",
                                        "organization_id": org["id"]})
        rink = self._post(c, "rink", {"venue_id": venue["id"],
                                      "name": f"{tag}-Rink"})
        slot = self._post(c, "ice-slot",
                          {"rink_id": rink["id"],
                           "start_time": "2027-05-04T18:00:00+00:00",
                           "end_time": "2027-05-04T19:30:00+00:00"})
        official = self._post(c, "official", {"name": f"{tag}-Official"})
        return {"pending_link_organizations": org["id"],
                "pending_link_venues": venue["id"],
                "pending_link_rinks": rink["id"],
                "pending_link_ice_slots": slot["id"],
                "pending_link_officials": official["id"]}

    def test_http_two_real_sessions_on_a_zero_program_install(self):
        """Two real Arena Manager sessions plus the League Admin, on a
        cold-boot install with no Program at all. Each sees only what IT
        created; nobody sees the installation."""
        self.assertEqual(
            self.srv.STATE.api.store.all_programs(), [],
            "the clean-slate server is not Program-less, so this test does "
            "not exercise the bootstrap branch")

        admin = self._login("admin")
        for username, password in (("zpb_http_a", "correct-horse-h1"),
                                   ("zpb_http_b", "correct-horse-h2")):
            status, acct = self._req(admin, "POST", "/api/accounts",
                                     {"username": username,
                                      "password": password,
                                      "role": "arena_manager", "scope": {}})
            self.assertEqual(status, 200, acct)
            self.assertNotIn("error", acct, acct)

        session_a = self._login("zpb_http_a", "correct-horse-h1")
        session_b = self._login("zpb_http_b", "correct-horse-h2")
        mine = self._arena_inventory(session_a, "HTTPMGRA")
        theirs = self._arena_inventory(session_b, "HTTPMGRB")
        # A Club, which only the League Admin may create — so the third
        # identity in this install also has something to leak.
        club = self._post(admin, "club", {"name": "HTTPBOSS-Club"})

        for session, own, foreign, own_tag, foreign_tags in (
                (session_a, mine, theirs, "HTTPMGRA", ("HTTPMGRB", "HTTPBOSS")),
                (session_b, theirs, mine, "HTTPMGRB", ("HTTPMGRA", "HTTPBOSS"))):
            ov = self._overview(session)
            for key in _SCOPED_KEYS:
                self.assertEqual(
                    ov[key], [],
                    f"a real Arena Manager session received a non-empty "
                    f"`{key}` on a Program-less installation -- the read fell "
                    "through to the unfiltered installation-wide shape")
            for key, rid in own.items():
                self.assertIn(rid, self._ids(ov, key),
                              (own_tag, key, "own creations must survive"))
            for key, rid in foreign.items():
                self.assertNotIn(
                    rid, self._ids(ov, key),
                    f"the {own_tag} session enumerated a {key} row created by "
                    "a different real session")
                self.assertNotIn(rid, self._ids(ov, _PENDING_TO_SCOPED[key]),
                                 (own_tag, key, "leaked through scoped list"))
            self.assertNotIn(
                club["id"], self._ids(ov, "pending_link_clubs"),
                f"the {own_tag} session received the League Admin's Club")
            blob = json.dumps(ov)
            for tag in foreign_tags:
                self.assertNotIn(
                    tag, blob,
                    f"{tag!r} appears in the {own_tag} session's payload -- a "
                    "foreign pre-Program record leaked by name")

        # The League Admin is not privileged here either: it holds every setup
        # permission in the installation and STILL receives only its own row.
        ov_admin = self._overview(admin)
        self.assertEqual(self._ids(ov_admin, "pending_link_clubs"),
                         {club["id"]}, ov_admin["pending_link_clubs"])
        blob = json.dumps(ov_admin)
        for tag in ("HTTPMGRA", "HTTPMGRB"):
            self.assertNotIn(
                tag, blob,
                f"the League Admin enumerated the {tag} Arena Manager's "
                "pre-Program records; the read permission is not ownership")

        # No session at all is the `user_id=None` case at the route boundary:
        # refused outright rather than answered with the install's inventory.
        status, body = self._req(self._client(), "GET",
                                 "/api/v2/setup/overview")
        self.assertEqual(status, 401,
                         f"an unauthenticated read of the zero-Program setup "
                         f"overview was not refused: {status} {body}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
