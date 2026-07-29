"""iCal calendar feeds (#82/#33).

A revocable bearer token scopes a public iCal feed to one actor. Team,
division, and player feeds are fixtures only (no roster/player names); the
official feed covers only that official's assigned games. Only the token
hash is stored.

#33 adds a division scope and a way for an anonymous visitor to mint a
team/division subscription straight from the public portal — team and
division feeds are exactly as public-safe as the existing unauthenticated
/api/public/schedule, so minting one needs no more authority than reading
that page. Player/official feeds stay behind the existing owner-or-operator
gate (a junior's schedule is not safe to hand out from an anonymous page).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player
from hockey_scheduler.domain.enums import IceSlotStatus, IceSlotType
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import build_ics, games_for_actor, hash_feed_token

UTC = timezone.utc


class CalendarServiceTest(unittest.TestCase):
    def setUp(self):
        self.store, self.gid, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.home = self.ids["home_team_id"]
        self.away = self.ids["away_team_id"]
        self.ref = self.ids["referee_id"]
        self.player = self.ids["selected_player_id"]

    def test_team_feed_only_includes_that_teams_games(self):
        games = games_for_actor(self.store, "team", self.home)
        self.assertTrue(games)
        for g in games:
            self.assertIn(self.home, (g.home_team_id, g.away_team_id))

    def test_official_feed_only_assigned_games(self):
        games = games_for_actor(self.store, "official", self.ref)
        assigned = {a.game_id
                    for a in self.store.assignments_for_official(self.ref)
                    if a.status.is_active}
        self.assertTrue(assigned)
        self.assertEqual({g.id for g in games}, assigned)

    def test_player_feed_uses_players_team(self):
        p = self.store.get_player(self.player)
        games = games_for_actor(self.store, "player", self.player)
        for g in games:
            self.assertIn(p.team_id, (g.home_team_id, g.away_team_id))

    def test_ics_is_wellformed_and_leaks_no_player_names(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        ics = build_ics(self.store, "team", self.home, now)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("END:VCALENDAR", ics)
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("SUMMARY:", ics)
        names = [p.name for p in self.store._query(Player)] \
            if hasattr(self.store, "_query") else \
            [p.name for p in self.store.players.values()]
        for name in names:
            self.assertNotIn(name, ics)

    def test_official_feed_carries_role_note(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        ics = build_ics(self.store, "official", self.ref, now)
        self.assertIn("Role:", ics)

    # -- division scope (#33) -----------------------------------------------
    def test_division_feed_only_includes_that_divisions_games(self):
        div = self.store.get_game(self.gid).division_id
        games = games_for_actor(self.store, "division", div)
        self.assertTrue(games)
        for g in games:
            self.assertEqual(g.division_id, div)

    def test_division_ics_is_wellformed_and_leaks_no_player_names(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        div = self.store.get_game(self.gid).division_id
        ics = build_ics(self.store, "division", div, now)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("X-WR-CALNAME:Division calendar",
                      build_ics(self.store, "division", div, now,
                               calendar_name="Division calendar"))
        for p in self.store.players.values():
            self.assertNotIn(p.name, ics)

    # -- reschedule / cancellation must update the SAME event (#33) ---------
    def _free_game_slot(self, exclude_id):
        game = self.store.get_game(self.gid)
        return next(
            s for s in self.store.all_ice_slots()
            if s.rink_id == self.store.get_ice_slot(game.ice_slot_id).rink_id
            and s.slot_type == IceSlotType.GAME
            and s.status == IceSlotStatus.AVAILABLE
            and s.id != exclude_id)

    def test_reschedule_updates_same_calendar_event_not_duplicate(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        uid = f"UID:{self.gid}@hockey-scheduler"
        before = build_ics(self.store, "team", self.home, now)
        self.assertEqual(before.count(uid), 1)
        before_start = next(l for l in before.split("\r\n")
                            if l.startswith("DTSTART"))

        game = self.store.get_game(self.gid)
        new_slot = self._free_game_slot(game.ice_slot_id)
        # Mirrors decide_reschedule's approve path (#29): move_game then
        # publish_game — a raw move_game alone unpublishes pending review, so
        # the fixture would (correctly) vanish from the public feed rather
        # than duplicate; republishing is what puts the SAME event back with
        # its new time.
        self.api.move_game(self.gid, new_slot.id, actor_id="user_admin")
        self.api.publish_game(self.gid, actor_id="user_admin")

        after = build_ics(self.store, "team", self.home, now)
        self.assertEqual(after.count(uid), 1)  # still exactly one VEVENT
        after_start = next(l for l in after.split("\r\n")
                           if l.startswith("DTSTART"))
        self.assertNotEqual(before_start, after_start)

    def test_cancelled_game_marked_cancelled_not_removed_or_duplicated(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        uid = f"UID:{self.gid}@hockey-scheduler"
        self.assertIn("STATUS:CONFIRMED",
                      build_ics(self.store, "team", self.home, now))

        self.api.cancel_game(self.gid, actor_id="user_admin")

        ics = build_ics(self.store, "team", self.home, now)
        self.assertEqual(ics.count(uid), 1)
        # The event stays in the feed (so a subscribed calendar app removes
        # its own copy via STATUS, per RFC 5545 convention for a
        # METHOD:PUBLISH polling feed) rather than silently disappearing.
        events = ics.split("BEGIN:VEVENT")
        this_event = next(e for e in events if uid in e)
        self.assertIn("STATUS:CANCELLED", this_event)

    # -- audit trail (#82) -------------------------------------------------
    def test_mint_writes_audit_without_token_material(self):
        before = len(self.store.all_setup_audit())
        row = self.api.create_calendar_feed_token(
            "team", self.home, label="Home sync", actor_id="user_admin")
        entries = self.store.all_setup_audit()
        self.assertEqual(len(entries), before + 1)
        e = entries[-1]
        self.assertEqual(e.action, "calendar_feed_token_created")
        self.assertEqual(e.entity_type, "calendar_feed_token")
        self.assertEqual(e.entity_id, row["id"])
        self.assertEqual(e.actor_id, "user_admin")
        self.assertEqual(e.detail["actor_type"], "team")
        self.assertEqual(e.detail["actor_ref"], self.home)
        self.assertEqual(e.detail["label"], "Home sync")
        # The audit record must never carry the raw token or its hash.
        blob = json.dumps(e.detail)
        self.assertNotIn(row["token"], blob)
        self.assertNotIn("token_hash", blob)
        self.assertNotIn(hash_feed_token(row["token"]), blob)

    def test_revoke_writes_audit(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        before = len(self.store.all_setup_audit())
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        e = self.store.all_setup_audit()[-1]
        self.assertEqual(len(self.store.all_setup_audit()), before + 1)
        self.assertEqual(e.action, "calendar_feed_token_revoked")
        self.assertEqual(e.entity_type, "calendar_feed_token")
        self.assertEqual(e.entity_id, row["id"])
        self.assertEqual(e.actor_id, "user_admin")
        self.assertFalse(e.detail["already_revoked"])
        blob = json.dumps(e.detail)
        self.assertNotIn(row["token"], blob)
        self.assertNotIn("token_hash", blob)

    def test_repeat_revoke_flags_already_revoked(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        e = self.store.all_setup_audit()[-1]
        self.assertEqual(e.action, "calendar_feed_token_revoked")
        self.assertTrue(e.detail["already_revoked"])  # idempotent no-op recorded

    # -- lifecycle metadata (#131) -------------------------------------------
    def test_signed_in_mint_records_created_by(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.assertEqual(row["created_by"], "user_admin")

    def test_anonymous_mint_records_created_by_as_anonymous(self):
        # No actor_id — mirrors the public /api/public/calendar-feeds route,
        # which never passes one since it has no session to resolve.
        row = self.api.create_calendar_feed_token("team", self.home)
        self.assertEqual(row["created_by"], "anonymous")

    def test_revoke_records_revoked_by(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.assertIsNone(row["revoked_by"])
        revoked = self.api.revoke_calendar_feed_token(
            row["id"], actor_id="user_admin")
        self.assertEqual(revoked["revoked_by"], "user_admin")

    def test_fresh_token_has_never_been_used(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.assertIsNone(row["last_used_at"])

    def test_fetching_the_ics_bumps_last_used_at(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.assertIsNone(row["last_used_at"])
        self.api.calendar_feed_ics("team", row["token"])
        refreshed = self.api.list_calendar_feed_tokens(
            "team", self.home)["feed_tokens"]
        mine = next(t for t in refreshed if t["id"] == row["id"])
        self.assertIsNotNone(mine["last_used_at"])

    def test_revoked_token_fetch_does_not_bump_last_used_at(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        ics = self.api.calendar_feed_ics("team", row["token"])
        self.assertIsNone(ics)  # revoked → no content
        refreshed = self.api.list_calendar_feed_tokens(
            "team", self.home)["feed_tokens"]
        mine = next(t for t in refreshed if t["id"] == row["id"])
        self.assertIsNone(mine["last_used_at"])


class CalendarHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.home = srv.STATE.ids["home_team_id"]
        cls.ref = srv.STATE.ids["referee_id"]
        cls.player = srv.STATE.ids["selected_player_id"]
        cls.division = srv.STATE.ids["division_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Every test method in this class shares one server/rate-limiter
        # instance and connects from the same loopback IP, so without this
        # reset a real per-caller ceiling (#131) would trip from unrelated
        # tests' request counts, not this test's own behavior.
        self.srv.RATE_LIMITER.reset()

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
                ctype = r.headers.get("Content-Type", "")
                raw = r.read()
                body = json.loads(raw or b"{}") if "json" in ctype else raw.decode()
                return r.status, ctype, body
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), \
                json.loads(e.read() or b"{}")

    def test_valid_token_returns_ics_and_revoked_returns_404(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        status, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(status, 200)
        token = created["token"]
        self.assertNotIn("token_hash", created)

        # Public, unauthenticated fetch of the feed.
        pub = urllib.request.build_opener()
        s, ctype, text = self._reqx(pub, f"/calendar/team/{token}.ics")
        self.assertEqual(s, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("BEGIN:VCALENDAR", text)

        # Revoke → the feed 404s.
        self._req(admin, "POST",
                  f"/api/calendar-feeds/{created['id']}/revoke")
        s2, _, _ = self._reqx(pub, f"/calendar/team/{token}.ics")
        self.assertEqual(s2, 404)

    def _reqx(self, opener, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with opener.open(req) as r:
                return r.status, r.headers.get("Content-Type", ""), \
                    r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    def test_unknown_and_mismatched_tokens_404(self):
        pub = urllib.request.build_opener()
        self.assertEqual(self._reqx(pub, "/calendar/team/bogus.ics")[0], 404)
        # A token minted for a team must not resolve on the official route.
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        _, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(
            self._reqx(pub, f"/calendar/official/{created['token']}.ics")[0], 404)

    def test_list_never_exposes_token_material(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        self._req(admin, "POST", "/api/calendar-feeds",
                  {"actor_type": "team", "actor_ref": self.home})
        status, _, body = self._req(
            admin, "GET",
            f"/api/calendar-feeds?actor_type=team&actor_ref={self.home}")
        self.assertEqual(status, 200)
        blob = json.dumps(body)
        self.assertNotIn("token_hash", blob)
        for t in body["feed_tokens"]:
            self.assertNotIn("token", t)

    def test_mint_and_revoke_over_http_write_audit(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        # #369 review: /api/demo/overview's setup_audit now scopes to the
        # caller's ACTIVE Program -- this class shares one server/store
        # across every test method (setUpClass runs once), and the admin
        # account's active-context fallback is not guaranteed to keep
        # landing on the Alpine Program `self.home` belongs to as other
        # tests run. Pin it explicitly rather than relying on the
        # auto-fallback, so this audit-visibility assertion is deterministic.
        self._req(admin, "POST", "/api/context",
                  {"program_id": self.srv.STATE.ids["league_id"]})
        _, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home,
             # A forged actor_id in the body must be ignored (server resolves).
             "actor_id": "attacker"})
        _, _, ov = self._req(admin, "GET", "/api/demo/overview")
        actions = [a["action"] for a in ov["setup_audit"]]
        self.assertIn("calendar_feed_token_created", actions)
        # And the created audit points at the real token id, not the raw token.
        mint = [a for a in ov["setup_audit"]
                if a["action"] == "calendar_feed_token_created"][-1]
        self.assertEqual(mint["entity_id"], created["id"])
        # The overview audit never carries the raw token string itself.
        self.assertNotIn(created["token"], json.dumps(ov["setup_audit"]))

        self._req(admin, "POST", f"/api/calendar-feeds/{created['id']}/revoke")
        _, _, ov2 = self._req(admin, "GET", "/api/demo/overview")
        self.assertIn("calendar_feed_token_revoked",
                      [a["action"] for a in ov2["setup_audit"]])

    def test_user_cannot_manage_another_actors_feed(self):
        coach = self._client()
        self._req(coach, "POST", "/api/auth/login",
                  {"username": "coach", "password": "demo"})
        # Coach owns the home team; a different team is forbidden.
        status, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": "team_elsewhere"})
        self.assertEqual(status, 403)
        # Own team is allowed.
        ok, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(ok, 200)

    # -- role-specific feed ownership (#82) --------------------------------
    def _login(self, username):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": "demo"})
        return c

    def test_player_manages_own_player_feed_but_not_team_feed(self):
        player = self._login("player")
        # Own player feed: create, list, revoke all allowed.
        st, _, created = self._req(
            player, "POST", "/api/calendar-feeds",
            {"actor_type": "player", "actor_ref": self.player})
        self.assertEqual(st, 200)
        st_l, _, _ = self._req(
            player, "GET",
            f"/api/calendar-feeds?actor_type=player&actor_ref={self.player}")
        self.assertEqual(st_l, 200)
        st_r, _, _ = self._req(
            player, "POST", f"/api/calendar-feeds/{created['id']}/revoke")
        self.assertEqual(st_r, 200)
        # The shared team feed — even for the player's OWN team — is forbidden
        # on create, list, and revoke (the player carries team_id in scope but
        # does not own the team resource).
        st_c, _, _ = self._req(
            player, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(st_c, 403)
        st_gl, _, _ = self._req(
            player, "GET",
            f"/api/calendar-feeds?actor_type=team&actor_ref={self.home}")
        self.assertEqual(st_gl, 403)

    def test_coach_cannot_manage_player_feed(self):
        coach = self._login("coach")
        st, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "player", "actor_ref": self.player})
        self.assertEqual(st, 403)

    def test_operator_manages_any_actor_feed(self):
        admin = self._login("admin")
        for actor_type, ref in (("team", self.home), ("official", self.ref),
                                ("player", self.player)):
            st, _, _ = self._req(
                admin, "POST", "/api/calendar-feeds",
                {"actor_type": actor_type, "actor_ref": ref})
            self.assertEqual(st, 200, f"operator denied {actor_type}")

    def test_player_cannot_revoke_coach_created_team_feed(self):
        # The critical regression: a coach mints a team feed; a player on that
        # same team must not be able to revoke it via the API.
        coach = self._login("coach")
        _, _, team_feed = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertIn("id", team_feed)

        player = self._login("player")
        st, _, _ = self._req(
            player, "POST", f"/api/calendar-feeds/{team_feed['id']}/revoke")
        self.assertEqual(st, 403)
        # And the feed is still live: the coach's token still resolves.
        pub = urllib.request.build_opener()
        s, ctype, _ = self._reqx(pub, f"/calendar/team/{team_feed['token']}.ics")
        self.assertEqual(s, 200)
        self.assertIn("text/calendar", ctype)

    # -- public-portal calendar subscription, no session (#33) --------------
    def test_anonymous_visitor_can_mint_team_and_division_feeds(self):
        anon = urllib.request.build_opener()
        for actor_type, ref in (("team", self.home), ("division", self.division)):
            st, _, body = self._req(
                anon, "POST", "/api/public/calendar-feeds",
                {"actor_type": actor_type, "actor_ref": ref})
            self.assertEqual(st, 200, f"anonymous mint denied for {actor_type}")
            self.assertIn("token", body)
            self.assertNotIn("token_hash", body)
            # And the resulting subscription URL is itself fetchable, no
            # session, exactly like a real calendar app would poll it.
            s, ctype, ics = self._reqx(anon, body["url"])
            self.assertEqual(s, 200)
            self.assertIn("text/calendar", ctype)
            self.assertIn("BEGIN:VCALENDAR", ics)

    def test_public_mint_rejects_player_and_official_scopes(self):
        anon = urllib.request.build_opener()
        for actor_type, ref in (("player", self.player), ("official", self.ref)):
            st, _, body = self._req(
                anon, "POST", "/api/public/calendar-feeds",
                {"actor_type": actor_type, "actor_ref": ref})
            self.assertEqual(st, 400, f"anonymous mint unexpectedly allowed for {actor_type}")
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_public_mint_rejects_unknown_team_or_division(self):
        anon = urllib.request.build_opener()
        st1, _, _ = self._req(
            anon, "POST", "/api/public/calendar-feeds",
            {"actor_type": "team", "actor_ref": "team_does_not_exist"})
        self.assertEqual(st1, 404)
        st2, _, _ = self._req(
            anon, "POST", "/api/public/calendar-feeds",
            {"actor_type": "division", "actor_ref": "division_does_not_exist"})
        self.assertEqual(st2, 404)

    def test_publicly_minted_feed_still_governed_by_the_real_owner_and_operator(self):
        # A publicly-minted token is not a separate, ungoverned concept — it's
        # the same CalendarFeedToken row an operator or the team's own coach
        # already manages via the authenticated routes (#82), so it can be
        # found and revoked through the exact same list/revoke flow.
        anon = urllib.request.build_opener()
        _, _, minted = self._req(
            anon, "POST", "/api/public/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})

        coach = self._login("coach")
        st_l, _, listing = self._req(
            coach, "GET",
            f"/api/calendar-feeds?actor_type=team&actor_ref={self.home}")
        self.assertEqual(st_l, 200)
        self.assertIn(minted["id"], [t["id"] for t in listing["feed_tokens"]])

        st_r, _, _ = self._req(
            coach, "POST", f"/api/calendar-feeds/{minted['id']}/revoke")
        self.assertEqual(st_r, 200)
        s, _, _ = self._reqx(anon, minted["url"])
        self.assertEqual(s, 404)

    def test_division_feed_route_accepts_the_division_actor_type(self):
        anon = urllib.request.build_opener()
        _, _, minted = self._req(
            anon, "POST", "/api/public/calendar-feeds",
            {"actor_type": "division", "actor_ref": self.division})
        s, ctype, ics = self._reqx(anon, f"/calendar/division/{minted['token']}.ics")
        self.assertEqual(s, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("BEGIN:VCALENDAR", ics)
        # Wrong actor_type on the route for a division-scoped token → 404
        # (mirrors the existing team/official mismatch guarantee).
        s2, _, _ = self._reqx(anon, f"/calendar/team/{minted['token']}.ics")
        self.assertEqual(s2, 404)

    def test_public_mint_rejects_non_string_actor_ref_instead_of_crashing(self):
        # Regression: a JSON array/object/number as actor_ref must not reach
        # a raw, unhandled store lookup (self-review, #33) — that killed the
        # request thread with no HTTP response at all, anonymously.
        anon = urllib.request.build_opener()
        for bad_ref in (["x"], {"a": 1}, 12345):
            st, _, body = self._req(
                anon, "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": bad_ref})
            self.assertEqual(st, 400, f"actor_ref={bad_ref!r} did not 400 cleanly")
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_public_mint_rejects_non_string_label_instead_of_crashing(self):
        # Regression: a JSON array/object as label must not reach the raw
        # CalendarFeedToken.label field and hit an unstructured store/DB
        # error further downstream (PR #129 review).
        anon = urllib.request.build_opener()
        home = self.home
        for bad_label in ([], {"a": 1}):
            st, _, body = self._req(
                anon, "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": home, "label": bad_label})
            self.assertEqual(st, 400, f"label={bad_label!r} did not 400 cleanly")
            self.assertEqual(body["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
