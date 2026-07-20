"""Import re-supply reactivates an edit-retired Player email (#268 review).

The audited edit (#268) can retire a Player's email contact (``active=False``,
kept for history/preferences). Both Player import paths must then treat a
re-supplied nonblank address as a full set: validate, update, AND reactivate
the single ``player:<id>`` EMAIL contact — otherwise the import reports success
while ``/api/players`` and notification delivery still see no email. A blank
import cell stays a no-op (it never reactivates or clears).

Proven on Memory / SQLite / PostgreSQL for the legacy teams+players import and
the hierarchy import's player upsert: after edit-retire → re-import, the contact
is active, operator-visible (``active_player_email``), and delivery-selected
(``resolve_destination`` returns the real address, not the placeholder), with
notification preferences preserved and the row never duplicated.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, LeagueSeason, NotificationChannel, Position, Team)
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.delivery import resolve_destination
from hockey_scheduler.store import InMemoryStore, SqlStore

EMAIL = NotificationChannel.EMAIL


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _contact(store, pid):
    return store.get_contact_destination(f"player:{pid}", EMAIL)


def _contacts(store, ref):
    return [c for c in store.all_contact_destinations()
            if c.recipient_ref == ref]


_TEAMS_CSV = ("team_code,team_name,club_name,division_name\n"
              "T1,Team One,Lions,U16\n")


def _players_csv(*rows):
    header = ("player_code,first_name,last_name,team_code,"
              "jersey_number,position,email\n")
    return header + "".join(r + "\n" for r in rows)


class LegacyImportReactivatesRetiredEmailTest(unittest.TestCase):
    """teams+players CSV commit path (commit_teams_players_import)."""

    def _seed(self, store):
        setup = SetupService(store)
        api = ApiService(store)
        program = setup.create_program("L", actor_id="a")
        season = setup.create_season(program.id, "S", actor_id="a")
        setup.create_league(season.id, "L1", actor_id="a")
        api.commit_teams_players_import(
            season.id,
            {"teams_csv": _TEAMS_CSV,
             "players_csv": _players_csv("P1,Ann,X,T1,7,forward,ann@x.com")},
            actor_id="a")
        pid = next(p.id for p in store.all_players()
                   if p.external_ref == "P1")
        return setup, api, season, pid

    def test_reimport_reactivates_retired_email_and_is_deliverable(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season, pid = self._seed(store)
                ref = f"player:{pid}"
                # A preference set before retirement must survive the round-trip.
                api.set_notification_preference(ref, "email", False)

                # The edit retires the email (kept, not deleted).
                setup.update_player(pid, email=None, actor_id="a")
                self.assertFalse(_contact(store, pid).active, label)
                self.assertIsNone(setup.active_player_email(pid), label)
                # Delivery falls back to the placeholder while retired.
                self.assertNotEqual(
                    resolve_destination(store, ref, EMAIL), "ann@x.com", label)

                # Re-importing the same address must revive the contact.
                api.commit_teams_players_import(
                    season.id,
                    {"teams_csv": _TEAMS_CSV,
                     "players_csv": _players_csv(
                         "P1,Ann,X,T1,7,forward,ann@x.com")},
                    actor_id="a")

                c = _contact(store, pid)
                self.assertTrue(c.active, label)                    # reactivated
                self.assertEqual(setup.active_player_email(pid),
                                 "ann@x.com", label)                # operator-visible
                self.assertEqual(resolve_destination(store, ref, EMAIL),
                                 "ann@x.com", label)                # delivery-selected
                self.assertEqual(len(_contacts(store, ref)), 1, label)  # not duplicated
                prefs = [p for p in store.all_notification_preferences()
                         if p.recipient_ref == ref]
                self.assertTrue(prefs, label)                       # preserved
                if isinstance(store, SqlStore):
                    store.close()

    def test_blank_import_cell_is_noop_and_never_reactivates(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season, pid = self._seed(store)
                setup.update_player(pid, email=None, actor_id="a")   # retire
                # A blank email cell is a no-op: it must NOT revive the contact.
                api.commit_teams_players_import(
                    season.id,
                    {"teams_csv": _TEAMS_CSV,
                     "players_csv": _players_csv("P1,Ann,X,T1,7,forward,")},
                    actor_id="a")
                self.assertFalse(_contact(store, pid).active, label)  # still retired
                self.assertIsNone(setup.active_player_email(pid), label)
                if isinstance(store, SqlStore):
                    store.close()


class HierarchyImportReactivatesRetiredEmailTest(unittest.TestCase):
    """Hierarchy import's player upsert (upsert_imported_player)."""

    def _seed(self, store):
        store.add_league_season(
            LeagueSeason(id="ls", league_id="l", season_id="se"))
        store.add_division(Division(id="d", league_season_id="ls", name="D1"))
        store.add_team(Team(id="home", name="Home", division="D1",
                            division_id="d"))
        setup = SetupService(store)
        player = setup.add_player("home", "Ann X", Position.FORWARD,
                                  jersey_number=7, email="ann@x.com",
                                  actor_id="a")
        return setup, player

    def test_upsert_with_new_email_reactivates_retired_contact(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, player = self._seed(store)
                pid = player.id
                ref = f"player:{pid}"
                setup.update_player(pid, email=None, actor_id="a")   # retire
                self.assertFalse(_contact(store, pid).active, label)

                # The hierarchy path upserts the player inside one transaction;
                # a new nonblank email must reactivate + update the contact.
                with store.transaction():
                    setup.upsert_imported_player(
                        "P1", player.name, "home", Position.FORWARD, 7,
                        "ann2@x.com", existing=player, actor_id="a")

                c = _contact(store, pid)
                self.assertTrue(c.active, label)                    # reactivated
                self.assertEqual(c.destination, "ann2@x.com", label)  # updated
                self.assertEqual(setup.active_player_email(pid),
                                 "ann2@x.com", label)               # operator-visible
                self.assertEqual(resolve_destination(store, ref, EMAIL),
                                 "ann2@x.com", label)               # delivery-selected
                self.assertEqual(len(_contacts(store, ref)), 1, label)  # not duplicated
                if isinstance(store, SqlStore):
                    store.close()

    def test_blank_upsert_email_is_noop_and_never_reactivates(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, player = self._seed(store)
                pid = player.id
                setup.update_player(pid, email=None, actor_id="a")   # retire
                # BOTH None and a blank string are import no-ops: the player
                # updates but the retired contact must NOT come back to life.
                for blank in (None, "", "   "):
                    with store.transaction():
                        setup.upsert_imported_player(
                            "P1", player.name, "home", Position.FORWARD, 7,
                            blank, existing=player, actor_id="a")
                    self.assertFalse(_contact(store, pid).active,
                                     (label, repr(blank)))         # still retired
                    self.assertIsNone(setup.active_player_email(pid),
                                      (label, repr(blank)))
                if isinstance(store, SqlStore):
                    store.close()

    def test_nonstring_email_rejected_before_any_player_write(self):
        # The import must reject a non-string/non-None email BEFORE mutating the
        # player — and the method itself must do so WITHOUT relying on an outer
        # transaction, so a direct caller never observes partial player state
        # (#268 review). No `with store.transaction()` wrapper here on purpose.
        from hockey_scheduler.domain.errors import ValidationError
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, player = self._seed(store)
                pid = player.id
                audits_before = len(store.all_setup_audit())
                for bad in (False, 0, ["a@b.co"], {"e": "a@b.co"}):
                    with self.assertRaises(ValidationError,
                                           msg=(label, repr(bad))) as ctx:
                        # A changed name in the same call must NOT land.
                        setup.upsert_imported_player(
                            "P1", "Renamed Import", "home", Position.FORWARD, 7,
                            bad, existing=player, actor_id="a")
                    self.assertEqual(ctx.exception.details.get("reason"),
                                     "invalid_email", (label, repr(bad)))
                    fresh = store.get_player(pid)
                    self.assertEqual(fresh.name, "Ann X", (label, repr(bad)))  # unchanged
                    # Pre-existing email untouched; not retired by a falsy value.
                    self.assertEqual(setup.active_player_email(pid), "ann@x.com",
                                     (label, repr(bad)))
                # Not one audit was written across all the rejected attempts.
                self.assertEqual(len(store.all_setup_audit()), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_new_player_with_bad_email_creates_no_player(self):
        # A brand-new imported row with a truthy non-string email rejects with
        # no player and no audit created (atomic, no outer transaction).
        from hockey_scheduler.domain.errors import ValidationError
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, _player = self._seed(store)
                before_players = len(store.players_for_team("home"))
                before_audits = len(store.all_setup_audit())
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    setup.upsert_imported_player(
                        "P2", "Newbie", "home", Position.FORWARD, 9,
                        ["a@b.co"], existing=None, actor_id="a")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_email", label)
                self.assertEqual(len(store.players_for_team("home")),
                                 before_players, label)            # no player added
                self.assertIsNone(
                    next((p for p in store.all_players()
                          if p.external_ref == "P2"), None), label)
                self.assertEqual(len(store.all_setup_audit()),
                                 before_audits, label)             # no audit
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
