"""``venue_access_missing`` at the HTTP and SERVICE boundaries — the domain
rule that outlived its UI path (#409, DEFECT 2, owner ruling).

WHY THIS FILE EXISTS.

Slice E's rule is that an IceSlot may only host a Game when the Game's own
Season holds an ACTIVE ``SeasonVenueAccess`` grant to the slot's Venue
(``league_scope.require_slot_belongs_to_season``). Until #409 that rule was
also *reachable from the shipped UI*, and ``e2e/venue-sharing.js`` step 5 drove
it: browse a Season that holds the grant, aim the Calendar wizard at a League
whose OWN Season does not, submit, and read the ``venue_access_missing``
refusal back off the wire.

#409 closed that route one rule earlier. A ``game`` is a SEASON-OWNED create,
so the create is refused unless the SELECTED Season IS the Season being written
into; and #369's Season ceiling hides the ice of every Venue the SELECTED
Season lacks a grant to. Standing in the game's own Season, the ungranted
Venue's ice is simply not on the Calendar to click. The owner's ruling was to
NOT manufacture an unreachable UI error: ``venue-sharing.js`` now proves the
AFFORDANCE IS ABSENT (see that file's step 5), and the domain rule is preserved
HERE, at the boundary that still enforces it.

So this file is the other half of that ruling. It is not a duplicate of
``test_league_ice_isolation.py``: that file proves the rule on the in-memory
store through the service objects only. What was lost with step 5 was the
END-TO-END, over-the-wire evidence, and that is what is rebuilt here:

  * the HTTP boundary — ``POST /api/v2/setup/game`` over a real socket with a
    real session cookie and a real persisted ActiveContext, plus
    ``POST /api/games/<id>/move`` and ``POST /api/games/<id>/publish``, which
    enforce the SAME rule on an already-existing Game;
  * the SERVICE boundary — ``ApiService.create_game``, called directly, so the
    rule is shown to live in the domain rather than in the HTTP gate that #409
    added in front of it;
  * ZERO WRITE and ZERO AUDIT — every refusal is compared against a snapshot of
    every entity table AND both audit logs (``SetupAuditLog`` and the game
    ``AuditLog``), so a refusal that half-wrote and then reported an error
    would fail here rather than in production;
  * a POSITIVE CONTROL for every refusal — the byte-identical call against a
    Venue the SAME Season IS granted must SUCCEED. Without it, every assertion
    below would be satisfied just as well by a route that is simply broken.

BOTH BRANCHES of the guard are covered, because they are different states and
only one of them is the obvious one:

  * NEVER GRANTED — ``season_venue_access_for_pair`` returns ``None``;
  * GRANTED THEN REVOKED — the row EXISTS with ``active = False``. This is the
    one a naive "is there a row?" implementation passes and the operator meets
    in real life, since revoking is how access ends.

THREE STORES. ``InMemoryStore``, SQLite and PostgreSQL. The guard reads a
grant row by (season, venue) and tests its ``active`` flag: a dict lookup on one
store, a real indexed query with real NULL/boolean semantics on the others, and
the revoked-grant branch in particular depends on the store returning the
INACTIVE row rather than nothing. A SKIP IS NOT A PASS — the PostgreSQL class
announces loudly when ``TEST_DATABASE_URL`` is unset.

FALSIFIER (measured, see the PR notes): delete the
``if access is None or not access.active:`` raise in
``services/league_scope.require_slot_belongs_to_season`` and every refusal test
below turns red on all three stores — the creates return 200 and durably write
Games onto ice their Season was never granted.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import AuditLog, Role
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None

# Every store table a refusal must leave untouched. Named explicitly rather
# than derived, so a new table has to be added here on purpose instead of
# silently escaping the residue assertion.
_TABLES = (
    ("programs", "all_programs"), ("seasons", "all_seasons"),
    ("leagues", "all_leagues"), ("league_seasons", "all_league_seasons"),
    ("divisions", "all_divisions"), ("teams", "all_teams"),
    ("players", "all_players"), ("games", "all_games"),
    ("clubs", "all_clubs"), ("officials", "all_officials"),
    ("venues", "all_venues"), ("rinks", "all_rinks"),
    ("ice_slots", "all_ice_slots"),
    ("organizations", "all_organizations"),
    ("registrations", "all_season_team_registrations"),
    ("season_venue_access", "all_season_venue_access"),
)


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


def _postgres_url():
    return os.environ.get("TEST_DATABASE_URL")


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg missing "
            "— the venue_access_missing boundary was NOT exercised on "
            "PostgreSQL. A SKIP HERE IS NOT A PASS: the guard reads a grant "
            "row by (season, venue) and tests its `active` flag, and the "
            "revoked-grant branch depends on the store returning the INACTIVE "
            "row rather than nothing — real boolean/NULL semantics that the "
            "in-memory dict lookup does not prove.")


class _VenueAccessHarness:
    """ONE Program, ONE Season, and THREE Venues in three different access
    states: GRANTED, NEVER GRANTED, and GRANTED-THEN-REVOKED.

    Everything else about the three is identical — same owning Organization,
    one Rink each, a bookable slot on each — so the ONLY thing that can differ
    in the answers below is the SeasonVenueAccess state. That is what makes the
    positive control a control rather than a second, easier scenario.
    """

    DATABASE_URL = None       # None -> InMemoryStore

    # -- harness -----------------------------------------------------------
    def setUp(self):
        if self.DATABASE_URL is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.DATABASE_URL
        srv.STATE.reset(seed=False)
        self.store = srv.STATE.api.store
        self._seq = 0
        self.w = self._world()

    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}-{uuid.uuid4().hex[:6]}"

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

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
        """A signed-in League Admin whose ActiveContext names the fixture's
        (Program, Season) — read BACK off the store, so every refusal below is
        provably about venue access and not about #409's own context gate
        refusing a Season the caller never selected."""
        username = f"va_{uuid.uuid4().hex[:10]}"
        srv.STATE.api.accounts.create_account(username, "demo", Role.LEAGUE_ADMIN)
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        status, raw, resp = self._req(
            c, "POST", "/api/context",
            {"program_id": self.w["program"], "season_id": self.w["season"]})
        self.assertEqual(status, 200, raw)
        self.assertEqual((resp.get("program") or {}).get("id"),
                         self.w["program"], raw)
        self.assertEqual((resp.get("season") or {}).get("id"),
                         self.w["season"], raw)
        return c

    # -- fixture -----------------------------------------------------------
    def _world(self):
        """Built through the setup service with ``actor_id`` only — no
        identity, so none of #409's create gates run while the fixture is laid
        down. Nobody has selected any of it yet."""
        svc = srv.STATE.api.setup
        facility = svc.create_organization("Facility Owner")
        operator = svc.create_organization("Program Operator")
        program = svc.create_program("Boundary Program",
                                     operator_organization_id=operator.id,
                                     timezone_name=TZ)
        season = svc.create_season(program.id, "Boundary Season")
        league = svc.create_league(season.id, "Boundary League")
        division = svc.create_division(season.id, "Boundary Div",
                                       league_id=league.id)
        club = svc.create_club("Boundary Club")
        home = svc.create_team(club_id=club.id, name="Home",
                               program_id=program.id, league_id=league.id)
        away = svc.create_team(club_id=club.id, name="Away",
                               program_id=program.id, league_id=league.id)
        # Registered, so a refused create is refused for VENUE ACCESS and never
        # for an unregistered team — a 400 `division_mismatch` would hide
        # whatever the venue guard actually decided.
        svc.register_team_for_season(season.id, home.id,
                                     division_id=division.id,
                                     league_id=league.id)
        svc.register_team_for_season(season.id, away.id,
                                     division_id=division.id,
                                     league_id=league.id)

        # Owned by the FACILITY organization and deliberately carrying NO
        # legacy Venue→Program link: Slice E's whole point is that a facility
        # owner and a Program operator are independent organizations, and
        # SeasonVenueAccess — not ownership — is what makes ice schedulable.
        def venue(name):
            v = svc.create_venue(name, organization_id=facility.id)
            return v, svc.create_rink(v.id, f"{name} Rink")

        v_granted, r_granted = venue("Granted Arena")
        v_ungranted, r_ungranted = venue("Ungranted Arena")
        v_revoked, r_revoked = venue("Revoked Arena")

        svc.grant_season_venue_access(season.id, v_granted.id)
        # GRANTED THEN REVOKED: the access row EXISTS and is INACTIVE. This is
        # the branch a "is there a row?" check passes and the operator actually
        # meets, since revoking is how access ends.
        revoked = svc.grant_season_venue_access(season.id, v_revoked.id)
        svc.revoke_season_venue_access(revoked.id)

        w = {"facility": facility.id, "operator": operator.id,
             "program": program.id, "season": season.id,
             "league": league.id, "division": division.id, "club": club.id,
             "home": home.id, "away": away.id,
             "granted_venue": v_granted.id, "granted_rink": r_granted.id,
             "ungranted_venue": v_ungranted.id, "ungranted_rink": r_ungranted.id,
             "revoked_venue": v_revoked.id, "revoked_rink": r_revoked.id}

        # NON-VACUITY OF THE FIXTURE ITSELF, asserted rather than assumed: the
        # three access states really are the three states this file claims.
        pair = self.store.season_venue_access_for_pair
        self.assertTrue(getattr(pair(season.id, v_granted.id), "active", False),
                        "the 'granted' Venue has no ACTIVE grant — the "
                        "positive control would be vacuous")
        self.assertIsNone(pair(season.id, v_ungranted.id),
                          "the 'ungranted' Venue already has a grant row")
        stale = pair(season.id, v_revoked.id)
        self.assertIsNotNone(stale, "the revoked grant row disappeared — this "
                                    "case has decayed into the never-granted "
                                    "one and no longer tests `active`")
        self.assertFalse(stale.active, "the revoked grant is still active")
        return w

    def _slot(self, rink_key):
        """A fresh bookable slot on one of the three Rinks. Made per call
        because two slots on one rink may not overlap and the game builder runs
        many times."""
        self._seq += 1
        start = (datetime(2031, 3, 1, tzinfo=timezone.utc)
                 + timedelta(days=self._seq))
        return srv.STATE.api.setup.create_ice_slot(
            self.w[rink_key], start, start + timedelta(minutes=90)).id

    def _game_body(self, slot_id):
        return {"season_id": self.w["season"], "league_id": self.w["league"],
                "division_id": self.w["division"],
                "home_team_id": self.w["home"], "away_team_id": self.w["away"],
                "ice_slot_id": slot_id}

    # -- observation -------------------------------------------------------
    def _all_audit(self):
        """Both audit logs, on all three stores. ``SetupAuditLog`` has a public
        ``all_setup_audit``; the game ``AuditLog`` has only per-game accessors,
        so a REFUSED create — which never gets an id to look up — needs the
        whole table. The in-memory store keeps it as a plain list."""
        rows = getattr(self.store, "audit", None)
        if rows is None:
            rows = self.store._query(AuditLog, order="id")
        return {(a.action, a.game_id, getattr(a, "actor", None), a.id)
                for a in rows}

    def _snapshot(self):
        snap = {name: {r.id for r in getattr(self.store, getter)()}
                for name, getter in _TABLES}
        snap["setup_audit"] = {(a.action, a.entity_id)
                               for a in self.store.all_setup_audit()}
        snap["game_audit"] = self._all_audit()
        # Not an id set: a refused MOVE would add no rows at all, so where each
        # existing Game sits is part of "nothing was written".
        snap["game_placement"] = {g.id: g.ice_slot_id
                                  for g in self.store.all_games()}
        return snap

    def _assert_no_residue(self, before, why):
        after = self._snapshot()
        for name in before:
            self.assertEqual(
                before[name], after[name],
                f"{why}: a REFUSED call left residue in `{name}` — "
                f"before {sorted(map(str, before[name]))}, "
                f"after {sorted(map(str, after[name]))}")

    def _assert_refusal(self, status, raw, resp, *, venue_key, slot_id, why):
        """The one shape every refusal in this file must have."""
        self.assertEqual(status, 400, f"{why}: expected a 400 refusal, got "
                                      f"HTTP {status}: {raw}")
        details = ((resp.get("error") or {}).get("details")) or {}
        self.assertEqual(details.get("reason"), "venue_access_missing",
                         f"{why}: the refusal is not venue_access_missing: {raw}")
        # Named BY ID, so the refusal can never quietly start being about some
        # other (Season, Venue) pair — the exact drift the journey step this
        # file replaces used to guard against.
        self.assertEqual(details.get("season_id"), self.w["season"], raw)
        self.assertEqual(details.get("venue_id"), self.w[venue_key], raw)
        self.assertEqual(details.get("ice_slot_id"), slot_id, raw)
        # Operator-actionable remediation (#271) survives with the rule.
        self.assertIn("remediation", details, raw)
        self.assertEqual(
            details.get("remediation_route"),
            f"/api/v2/setup/seasons/{self.w['season']}/venue-access", raw)


class _VenueAccessMixin(_VenueAccessHarness):

    # -- HTTP boundary: create ---------------------------------------------
    def test_http_create_on_a_never_granted_venue_is_refused(self):
        """The rule at the wire. Same operator, same Season, same teams — only
        the Venue's access state differs from the positive control below."""
        c = self._operator()
        slot = self._slot("ungranted_rink")
        before = self._snapshot()
        status, raw, resp = self._req(c, "POST", "/api/v2/setup/game",
                                      self._game_body(slot))
        self._assert_refusal(status, raw, resp, venue_key="ungranted_venue",
                             slot_id=slot, why="HTTP create / never granted")
        self._assert_no_residue(before, "HTTP create / never granted")

    def test_http_create_on_a_revoked_grant_is_refused(self):
        """The branch a row-existence check gets wrong: the grant row is there,
        it is just no longer ``active``."""
        c = self._operator()
        slot = self._slot("revoked_rink")
        before = self._snapshot()
        status, raw, resp = self._req(c, "POST", "/api/v2/setup/game",
                                      self._game_body(slot))
        self._assert_refusal(status, raw, resp, venue_key="revoked_venue",
                             slot_id=slot, why="HTTP create / revoked grant")
        self._assert_no_residue(before, "HTTP create / revoked grant")

    def test_http_create_succeeds_on_the_granted_venue(self):
        """THE POSITIVE CONTROL. The byte-identical request, differing only in
        which Rink the slot sits on, must SUCCEED and durably write the Game —
        otherwise both refusals above would be satisfied by a broken route."""
        c = self._operator()
        slot = self._slot("granted_rink")
        status, raw, resp = self._req(c, "POST", "/api/v2/setup/game",
                                      self._game_body(slot))
        self.assertEqual(status, 200, f"the create was refused on a Venue this "
                                      f"very Season IS granted: {raw}")
        self.assertNotIn("error", resp, raw)
        game = self.store.get_game(resp["id"])
        self.assertIsNotNone(game, "the accepted create wrote no Game row")
        self.assertEqual(game.ice_slot_id, slot,
                         "the accepted create landed on a different slot")

    # -- SERVICE boundary ---------------------------------------------------
    def test_service_create_refuses_the_ungranted_venue_with_a_positive_control(self):
        """The rule lives in the DOMAIN, not in the HTTP gate #409 put in front
        of it. ``ApiService.create_game`` is called directly — no session, no
        ActiveContext, none of #409's create comparisons — and must still
        refuse, write nothing, and still accept the granted Venue."""
        api = srv.STATE.api
        for venue_key, rink_key in (("ungranted_venue", "ungranted_rink"),
                                    ("revoked_venue", "revoked_rink")):
            with self.subTest(venue=venue_key):
                slot = self._slot(rink_key)
                before = self._snapshot()
                result = api.create_game(
                    self.w["season"], self.w["division"], self.w["home"],
                    self.w["away"], slot, league_id=self.w["league"])
                details = ((result.get("error") or {}).get("details")) or {}
                self.assertEqual(
                    details.get("reason"), "venue_access_missing",
                    f"the service facade did not refuse {venue_key}: {result}")
                self.assertEqual(details.get("season_id"), self.w["season"])
                self.assertEqual(details.get("venue_id"), self.w[venue_key])
                self.assertEqual(details.get("ice_slot_id"), slot)
                self._assert_no_residue(before, f"service create / {venue_key}")

        # POSITIVE CONTROL at the same boundary.
        ok_slot = self._slot("granted_rink")
        ok = api.create_game(self.w["season"], self.w["division"],
                             self.w["home"], self.w["away"], ok_slot,
                             league_id=self.w["league"])
        self.assertNotIn("error", ok,
                         f"the service facade refused a GRANTED Venue: {ok}")
        self.assertEqual(self.store.get_game(ok["id"]).ice_slot_id, ok_slot)

    # -- HTTP boundary: the rule on an EXISTING Game ------------------------
    def test_http_move_onto_an_ungranted_venue_is_refused(self):
        """The same rule guards RELOCATION, not just creation: a Game that
        legitimately exists on granted ice may not be moved onto ice its Season
        was never granted. The refused move must leave the Game exactly where
        it was — which is why the snapshot carries slot placement, not just
        row ids."""
        c = self._operator()
        created = self._req(c, "POST", "/api/v2/setup/game",
                            self._game_body(self._slot("granted_rink")))[2]
        self.assertNotIn("error", created, created)
        game_id, origin = created["id"], created["ice_slot_id"]

        for venue_key, rink_key in (("ungranted_venue", "ungranted_rink"),
                                    ("revoked_venue", "revoked_rink")):
            with self.subTest(venue=venue_key):
                target = self._slot(rink_key)
                before = self._snapshot()
                status, raw, resp = self._req(
                    c, "POST", f"/api/games/{game_id}/move",
                    {"ice_slot_id": target, "reason": "boundary probe"})
                self._assert_refusal(status, raw, resp, venue_key=venue_key,
                                     slot_id=target,
                                     why=f"HTTP move / {venue_key}")
                self._assert_no_residue(before, f"HTTP move / {venue_key}")
                self.assertEqual(
                    self.store.get_game(game_id).ice_slot_id, origin,
                    "the refused move relocated the Game anyway")

    def test_http_move_succeeds_onto_another_granted_slot(self):
        """THE POSITIVE CONTROL for the move path: the same operator, the same
        Game, the same verb — onto a second slot of the GRANTED Venue — must
        succeed and durably relocate the Game."""
        c = self._operator()
        created = self._req(c, "POST", "/api/v2/setup/game",
                            self._game_body(self._slot("granted_rink")))[2]
        self.assertNotIn("error", created, created)
        target = self._slot("granted_rink")
        status, raw, resp = self._req(
            c, "POST", f"/api/games/{created['id']}/move",
            {"ice_slot_id": target, "reason": "boundary control"})
        self.assertEqual(status, 200, f"a move onto GRANTED ice was refused: {raw}")
        self.assertNotIn("error", resp, raw)
        self.assertEqual(self.store.get_game(created["id"]).ice_slot_id, target,
                         "the accepted move did not relocate the Game")

    def test_http_publish_is_refused_once_the_grant_is_revoked(self):
        """The rule is re-checked at PUBLISH, so a Game that was legal when it
        was drafted cannot be published onto ice the Season has since lost. The
        state is built the only way an operator can build it — create on a live
        grant, then revoke it — and the refusal must leave the Game unpublished
        with no audit entry."""
        c = self._operator()
        slot = self._slot("granted_rink")
        created = self._req(c, "POST", "/api/v2/setup/game",
                            self._game_body(slot))[2]
        self.assertNotIn("error", created, created)
        game_id = created["id"]

        # POSITIVE CONTROL FIRST, while the grant is still live: publishing is
        # otherwise permitted for this Game, this operator, this slot.
        status, raw, resp = self._req(
            c, "POST", f"/api/games/{game_id}/publish", {})
        self.assertEqual(status, 200, f"publishing on GRANTED ice was refused: {raw}")
        self.assertTrue(self.store.get_game(game_id).published, raw)

        # Return the Game to draft FIRST (unpublishing is not gated — the guard
        # only runs on the publishing direction), then take the access away
        # exactly as the UI does, so the second publish attempt below differs
        # from the successful one above in the access state and nothing else.
        srv.STATE.api.setup.publish_game(game_id, published=False)
        self.assertFalse(self.store.get_game(game_id).published,
                         "the fixture could not return the Game to draft")
        access = self.store.season_venue_access_for_pair(
            self.w["season"], self.w["granted_venue"])
        srv.STATE.api.setup.revoke_season_venue_access(access.id)

        before = self._snapshot()
        status, raw, resp = self._req(
            c, "POST", f"/api/games/{game_id}/publish", {})
        self._assert_refusal(status, raw, resp, venue_key="granted_venue",
                             slot_id=slot, why="HTTP publish / revoked grant")
        self._assert_no_residue(before, "HTTP publish / revoked grant")
        self.assertFalse(self.store.get_game(game_id).published,
                         "the refused publish published the Game anyway")


# ---------------------------------------------------------------------------
# Concrete backends. A SKIP IS NOT A PASS.
# ---------------------------------------------------------------------------
class VenueAccessBoundaryMemoryTest(_VenueAccessMixin, unittest.TestCase):
    DATABASE_URL = None


class VenueAccessBoundarySqliteTest(_VenueAccessMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class VenueAccessBoundaryPostgresTest(_VenueAccessMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


if __name__ == "__main__":
    unittest.main()
