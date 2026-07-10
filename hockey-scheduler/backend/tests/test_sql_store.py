"""SQL store tests.

Run the real service/API flows against the SqlStore (SQLite adapter) to prove
parity with the in-memory store, plus reload tests that re-open the database and
confirm state persisted. The same SqlStore runs against PostgreSQL in CI via
DATABASE_URL.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.errors import NotFoundError
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import SqlStore, create_store

UTC = timezone.utc


class SqlStoreParityTest(unittest.TestCase):
    """The full demo + E2E substitute flow must work on the SQL store."""

    def setUp(self):
        self.store = SqlStore(":memory:")
        _, self.game_id, self.ids = build_full_demo_store(self.store)
        self.api = ApiService(self.store)

    def test_seed_and_overview(self):
        ov = self.api.get_demo_overview()
        self.assertEqual(ov["league"]["name"], "Alpine Ice Hockey League")
        # The pilot data pack (#97) grows this from the original 4-team demo
        # to 12 teams across the 3 divisions.
        self.assertEqual({t["name"] for t in ov["teams"]}, {
            "U16 Lions", "U16 Falcons", "U16 Wolves", "U16 Comets",
            "U16 Panthers", "U16 Sharks",
            "U18 Lions", "U18 Falcons", "U18 Wolves", "U18 Bears",
            "Senior Lions", "Senior Falcons",
        })
        # The core scenario's seeded game plus ~18 more published games (#97).
        self.assertEqual(len(ov["public_fixtures"]),
                         sum(1 for g in self.store.all_games() if g.published))

    def test_opens_confirmed(self):
        self.assertEqual(self.api.get_roster_status(self.game_id)["status"],
                         "roster_confirmed")

    def test_end_to_end_flow_on_sql(self):
        gid = self.game_id
        self.api.set_availability(gid, self.ids["selected_player_id"], "unavailable")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "open_slot")
        self.api.enroll_substitute(gid, self.ids["substitute_player_id"])
        self.assertEqual(self.api.get_roster_status(gid)["status"], "needs_substitute")
        self.api.add_substitute_to_roster(gid, self.ids["substitute_player_id"],
                                          actor_id="coach")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "roster_confirmed")
        self.api.lock_roster(gid, actor_id="coach")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "locked")

    def test_scheduling_rules_enforced_on_sql(self):
        ov = self.api.get_demo_overview()
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        # schedule a draft game, then double-book the slot → conflict
        self.api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                             u16[0]["id"], u16[1]["id"], slot["id"])
        dup = self.api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                                   u16[0]["id"], u16[1]["id"], slot["id"])
        self.assertEqual(dup["error"]["code"], "schedule_conflict")

    def test_per_recipient_read_state_on_sql(self):
        # The feed + per-recipient read state (#32/#57) must roundtrip through
        # the notifications_feed and notification_recipients tables.
        admin = self.api.get_notifications("league_admin", {})
        self.assertGreater(admin["unread"], 0)
        public = next(n for n in admin["notifications"]
                      if n["kind"] == "result_approved")
        self.api.mark_notification_read(public["id"], "league_admin", {})

        # Admin's copy persists as read; the viewer's copy stays unread.
        admin2 = self.api.get_notifications("league_admin", {})
        self.assertTrue(next(n for n in admin2["notifications"]
                             if n["id"] == public["id"])["read"])
        viewer = self.api.get_notifications("viewer", {})
        self.assertFalse(next(n for n in viewer["notifications"]
                              if n["id"] == public["id"])["read"])

    def test_per_user_read_state_on_sql(self):
        # Two accounts with the same role must not share read state (#69),
        # through the SQL store.
        public = next(n for n in self.api.get_notifications(
            "viewer", {}, user_id="user_x")["notifications"]
            if n["kind"] == "result_approved")
        self.api.mark_notification_read(
            public["id"], "viewer", {}, user_id="user_x")
        x = self.api.get_notifications("viewer", {}, user_id="user_x")
        y = self.api.get_notifications("viewer", {}, user_id="user_y")
        self.assertTrue(next(n for n in x["notifications"]
                             if n["id"] == public["id"])["read"])
        self.assertFalse(next(n for n in y["notifications"]
                              if n["id"] == public["id"])["read"])

    def test_delivery_queue_roundtrips_on_sql(self):
        # Emission fans out to notification_deliveries; the worker drains them
        # and the sent state persists through the SQL store (#58).
        ov = self.api.get_delivery_overview()
        self.assertGreater(ov["total"], 0)
        self.assertEqual(ov["by_status"].get("pending"), ov["total"])
        res = self.api.process_notification_deliveries()
        self.assertEqual(res["sent"], ov["total"])
        ov2 = self.api.get_delivery_overview()
        self.assertEqual(ov2["by_status"].get("sent"), ov2["total"])
        self.assertTrue(all(d["sent_at"] for d in ov2["deliveries"]))
        # Recipient targeting (#59) persists through the SQL store too.
        self.assertTrue(all(d["recipient_ref"] and d["destination"]
                            for d in ov2["deliveries"]))

    def test_contact_registry_roundtrips_on_sql(self):
        # A stored contact (#60) persists and overrides the placeholder on the
        # next emission, all through the SQL store.
        self.api.set_contact_destination(
            "scheduler", "email", "ops@contacts.invalid")
        listed = self.api.list_contact_destinations()["contacts"]
        self.assertEqual(listed[0]["destination"], "ops@contacts.invalid")
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        ov = self.api.get_delivery_overview()
        accepted_email = next(
            d for d in ov["deliveries"]
            if d["recipient_ref"] == "scheduler" and d["channel"] == "email")
        self.assertEqual(accepted_email["destination"], "ops@contacts.invalid")

    def test_device_token_registry_roundtrips_on_sql(self):
        # A registered device token (#65) persists and overrides the push
        # placeholder on the next emission, through the SQL store.
        self.api.register_device_token("scheduler", "fcm", "tok-sql")
        listed = self.api.list_device_tokens()["device_tokens"]
        self.assertEqual(listed[0]["token"], "tok-sql")
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        ov = self.api.get_delivery_overview()
        push = next(d for d in ov["deliveries"]
                    if d["recipient_ref"] == "scheduler" and d["channel"] == "push")
        self.assertEqual(push["destination"], "tok-sql")
        self.assertFalse(push["placeholder"])

    def test_user_account_roundtrips_on_sql(self):
        # A created account (#67) persists — hashed password, role, scope,
        # active flag — and login verification works through the SQL store.
        row = self.api.create_user_account(
            "sql_coach", "sql-pw", "coach", scope={"team_id": "team_sql"})
        self.assertNotIn("password_hash", row)
        listed = self.api.list_user_accounts()["user_accounts"]
        self.assertIn("sql_coach", [a["username"] for a in listed])
        self.assertIsNone(self.api.verify_login("sql_coach", "wrong"))
        verified = self.api.verify_login("sql_coach", "sql-pw")
        self.assertEqual(verified["role"], "coach")
        self.assertEqual(verified["scope"], {"team_id": "team_sql"})
        self.api.set_user_account_active(row["id"], False)
        self.assertIsNone(self.api.verify_login("sql_coach", "sql-pw"))

    def test_league_owner_and_venue_league_roundtrip_on_sql(self):
        # League.organization_id and Venue.league_id (#173) must persist through
        # their columns, and the venue must derive the league's owner.
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        self.assertEqual(self.store.get_league(league["id"]).organization_id, org["id"])
        stored_venue = self.store.get_venue(venue["id"])
        self.assertEqual(stored_venue.league_id, league["id"])
        self.assertEqual(stored_venue.organization_id, org["id"])

    def test_organization_and_venue_link_roundtrip_on_sql(self):
        # An organization (#166) persists through the organizations table, and
        # a venue's organization_id column roundtrips the ownership link.
        org = self.api.create_organization("Summit Ice Facilities",
                                           short_name="Summit")
        venue = self.api.create_venue("Ice Palace", organization_id=org["id"])
        self.assertEqual(venue["organization_id"], org["id"])
        stored = self.store.get_venue(venue["id"])
        self.assertEqual(stored.organization_id, org["id"])
        ov = self.api.get_demo_overview()
        self.assertIn(org["id"], [o["id"] for o in ov["organizations"]])
        row = next(v for v in ov["venues"] if v["id"] == venue["id"])
        self.assertEqual(row["organization_name"], "Summit Ice Facilities")

    def test_level_and_division_link_roundtrip_on_sql(self):
        # A level (#166) persists through the levels table, and a division's
        # level_id column roundtrips the level/tier link.
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_level(season["id"], "Level 1", sort_order=1)
        div = self.api.create_division(season["id"], "Div A", level_id=level["id"])
        self.assertEqual(div["level_id"], level["id"])
        stored = self.store.get_division(div["id"])
        self.assertEqual(stored.level_id, level["id"])
        ov = self.api.get_demo_overview()
        self.assertIn(level["id"], [lv["id"] for lv in ov["levels"]])
        row = next(d for d in ov["divisions"] if d["id"] == div["id"])
        self.assertEqual(row["level_name"], "Level 1")

    def test_setup_hierarchy_builds_on_sql(self):
        # The nested setup tree (#166 PR C / #173 PR B) must build from the SQL
        # store's flat tables: the owner nests its league → venues → rinks, and
        # the seeded level nests its divisions.
        h = self.api.get_setup_hierarchy()
        org = next(o for o in h["organizations"]
                   if o["name"] == "Summit Ice Facilities")
        self.assertTrue(org["leagues"])
        league = org["leagues"][0]
        self.assertTrue(league["venues"])
        self.assertIn("ice_slot_count", league["venues"][0]["rinks"][0])
        season = h["leagues"][0]["seasons"][0]
        self.assertIn("Junior Tier", [lv["name"] for lv in season["levels"]])
        self.assertIn("missing_assignments", h)

    def test_reassignment_persists_on_sql(self):
        # Moving a record under a new parent (#166 PR D) must UPDATE the row in
        # the SQL store, not just mutate an in-memory object.
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_level(season["id"], "Level 1")
        div = self.api.create_division(season["id"], "Div A")
        moved = self.api.assign_division_level(div["id"], level["id"])
        self.assertEqual(moved["level_id"], level["id"])
        # Re-read straight from the store to prove it persisted.
        self.assertEqual(self.store.get_division(div["id"]).level_id, level["id"])
        cleared = self.api.assign_division_level(div["id"], None)
        self.assertIsNone(cleared["level_id"])
        self.assertIsNone(self.store.get_division(div["id"]).level_id)


class SqlStoreTransactionTest(unittest.TestCase):
    """A failure mid multi-write operation must roll the whole thing back."""

    def test_failed_create_game_rolls_back(self):
        store = SqlStore(":memory:")
        svc = SetupService(store)
        league = svc.create_league("L")
        season = svc.create_season(league.id, "S")
        div = svc.create_division(season.id, "D")
        home = svc.create_team(svc.create_club("CA").id, div.id, "TA")
        away = svc.create_team(svc.create_club("CB").id, div.id, "TB")
        rink = svc.create_rink(
            svc.create_venue("V", league_id=league.id).id, "R")
        slot = svc.create_ice_slot(rink.id, datetime(2026, 9, 1, 18, 30, tzinfo=UTC),
                                   datetime(2026, 9, 1, 20, 0, tzinfo=UTC))
        self.assertEqual(len(store.all_games()), 0)

        # Force the final step of create_game (the audit write) to fail, after
        # the game insert and the slot-allocation update.
        def boom(*_a, **_k):
            raise RuntimeError("audit write failed")
        store.add_setup_audit = boom
        with self.assertRaises(RuntimeError):
            svc.create_game(season.id, div.id, home.id, away.id, slot.id)

        # The transaction rolled back: no game, and the slot is still available.
        self.assertEqual(len(store.all_games()), 0)
        self.assertEqual(store.get_ice_slot(slot.id).status.value, "available")


class SqlStoreReloadTest(unittest.TestCase):
    """State must survive re-opening the database (the whole point of #23).

    Runs against PostgreSQL when TEST_DATABASE_URL is set (CI), else a SQLite
    temp file locally.
    """

    def setUp(self):
        self._tmp = None
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).reset_schema()  # isolate from other tests
            self.url = url
        else:
            fd, self._tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            self.url = self._tmp

    def tearDown(self):
        if self._tmp:
            os.remove(self._tmp)

    def _api(self):
        return ApiService(SqlStore(self.url))

    def test_state_persists_across_reload(self):
        store = SqlStore(self.url)
        _, gid, ids = build_full_demo_store(store)
        api = ApiService(store)
        # Mutate: back out, add substitute, lock — and schedule + publish a 2nd game.
        api.set_availability(gid, ids["selected_player_id"], "unavailable")
        api.enroll_substitute(gid, ids["substitute_player_id"])
        api.add_substitute_to_roster(gid, ids["substitute_player_id"], actor_id="c")
        api.lock_roster(gid, actor_id="c")
        ov = api.get_demo_overview()
        before_public = len(ov["public_fixtures"])
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        g2 = api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                             u16[0]["id"], u16[1]["id"], slot["id"])
        api.publish_game(g2["id"], actor_id="c")
        used_slot = slot["id"]

        # Re-open the database in a fresh store/connection.
        api2 = self._api()
        # roster lock persisted
        self.assertEqual(api2.get_roster_status(gid)["status"], "locked")
        ov2 = api2.get_demo_overview()
        # the second game persisted and is published (in public fixtures)
        ids2 = {g["game_id"] for g in ov2["schedule"]}
        self.assertIn(g2["id"], ids2)
        self.assertEqual(len(ov2["public_fixtures"]), before_public + 1)
        # its ice slot is now allocated (allocation persisted)
        slot2 = next(s for s in ov2["ice_slots"] if s["id"] == used_slot)
        self.assertEqual(slot2["status"], "allocated")
        # league/season/divisions/teams persisted
        self.assertEqual(ov2["league"]["name"], "Alpine Ice Hockey League")
        self.assertEqual(len(ov2["teams"]), 12)  # 12-team pilot data pack (#97)

    def test_create_store_factory_selects_sql_for_url(self):
        store = create_store(self.url)
        self.assertIsInstance(store, SqlStore)


if __name__ == "__main__":
    unittest.main()
