"""Durable athlete identity (#273): one create/edit contract + private fields.

Covers, on Memory, SQLite, and PostgreSQL:

* structured names (first/last/preferred) with the display name DERIVED —
  the legacy flattened form stays supported, the two forms never mix, and
  no code path ever guesses a first/last split from a display name;
* the private birthdate and governing-body registration number: validated
  by the shared per-field validators, same-team registration duplicates
  hard-refused, and BOTH stripped from every default facade payload
  (AC[2]) with an explicit operator opt-in mirroring include_email;
* the 1-7 skill rating (owner ruling on #287 assigns the field here) with
  unrated (None) as a first-class state;
* duplicate WARNINGS: exact same-name-on-one-Team records lacking
  disambiguating data append a player_duplicate_warning audit and appear
  in player_duplicate_report — never a block, never a merge (AC[4]);
* audits name changed FIELDS only — no birthdate or registration value
  ever enters the audit trail.
"""

import json
import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Position, Program, Role, Team
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore


def _service_backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


class AthleteIdentityTestBase(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                store.add_team(Team(id="t1", name="Team One"))
                store.add_team(Team(id="t2", name="Team Two"))
                api = ApiService(store)
                try:
                    yield label, store, api, api.setup
                finally:
                    if isinstance(store, SqlStore):
                        store.close()


class CreatePlayerIdentityTest(AthleteIdentityTestBase):
    def test_structured_create_derives_display_name(self):
        for label, store, api, setup in self._each():
            p = setup.add_player(
                "t1", None, Position.FORWARD, first_name="  Jane ",
                last_name="van  der Berg", preferred_name=" JJ ",
                birthdate="2015-03-02", registration_number=" HC-12345 ",
                skill_rating=5, actor_id="a")
            self.assertEqual(p.first_name, "Jane", label)
            self.assertEqual(p.last_name, "van der Berg", label)
            self.assertEqual(p.name, "Jane van der Berg", label)
            self.assertEqual(p.preferred_name, "JJ", label)
            self.assertEqual(p.birthdate, "2015-03-02", label)
            self.assertEqual(p.registration_number, "HC-12345", label)
            self.assertEqual(p.skill_rating, 5, label)
            # Round-trips through the store unchanged.
            got = store.get_player(p.id)
            self.assertEqual(
                (got.first_name, got.last_name, got.birthdate,
                 got.registration_number, got.skill_rating),
                ("Jane", "van der Berg", "2015-03-02", "HC-12345", 5), label)

    def test_legacy_flattened_create_still_works_and_never_splits(self):
        for label, store, api, setup in self._each():
            p = setup.add_player("t1", "Jordan Lee", Position.GOALIE)
            self.assertEqual(p.name, "Jordan Lee", label)
            self.assertIsNone(p.first_name, label)
            self.assertIsNone(p.last_name, label)

    def test_conflicting_name_forms_refused(self):
        for label, store, api, setup in self._each():
            with self.assertRaises(ValidationError) as ctx:
                setup.add_player("t1", "Jane Smith", Position.FORWARD,
                                 first_name="Jane", last_name="Smith")
            self.assertEqual(ctx.exception.details["reason"],
                             "conflicting_name_forms", label)

    def test_half_structured_name_refused(self):
        for label, store, api, setup in self._each():
            with self.assertRaises(ValidationError) as ctx:
                setup.add_player("t1", None, Position.FORWARD,
                                 first_name="Jane")
            self.assertEqual(ctx.exception.details["reason"],
                             "invalid_last_name", label)

    def test_field_level_rejections_write_nothing(self):
        bad = [
            (dict(birthdate="2030-01-01"), "invalid_birthdate"),
            (dict(birthdate="2015-02-30"), "invalid_birthdate"),
            (dict(birthdate="03/02/2015"), "invalid_birthdate"),
            (dict(registration_number="HC 123"),
             "invalid_registration_number"),
            (dict(registration_number=42), "invalid_registration_number"),
            (dict(skill_rating=0), "invalid_skill_rating"),
            (dict(skill_rating=8), "invalid_skill_rating"),
            (dict(skill_rating=True), "invalid_skill_rating"),
            (dict(preferred_name=7), "invalid_preferred_name"),
        ]
        for label, store, api, setup in self._each():
            for kwargs, reason in bad:
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    setup.add_player("t1", None, Position.FORWARD,
                                     first_name="A", last_name="B", **kwargs)
                self.assertEqual(ctx.exception.details["reason"], reason,
                                 f"{label}: {kwargs}")
            self.assertEqual(store.all_players(), [], label)

    def test_same_team_registration_number_duplicate_refused(self):
        for label, store, api, setup in self._each():
            setup.add_player("t1", None, Position.FORWARD, first_name="A",
                             last_name="B", registration_number="HC-1")
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.add_player("t1", None, Position.FORWARD, first_name="C",
                                 last_name="D", registration_number="HC-1")
            self.assertEqual(ctx.exception.details["reason"],
                             "duplicate_registration_number", label)
            # Cross-team is allowed (legacy permanent Player->Team model) —
            # surfaced by the duplicate report, not refused.
            other = setup.add_player(
                "t2", None, Position.FORWARD, first_name="A", last_name="B",
                registration_number="HC-1")
            self.assertEqual(other.registration_number, "HC-1", label)


class DuplicateWarningTest(AthleteIdentityTestBase):
    def _warnings(self, store):
        return [a for a in store.all_setup_audit()
                if a.action == "player_duplicate_warning"]

    def test_same_name_same_team_without_disambiguation_warns(self):
        for label, store, api, setup in self._each():
            first = setup.add_player("t1", "Alex Kim", Position.FORWARD,
                                     actor_id="op")
            self.assertEqual(self._warnings(store), [], label)
            second = setup.add_player("t1", None, Position.GOALIE,
                                      first_name="alex", last_name="KIM",
                                      actor_id="op")
            warns = self._warnings(store)
            self.assertEqual(len(warns), 1, label)
            self.assertEqual(warns[0].entity_id, second.id, label)
            self.assertEqual(warns[0].detail["matching_player_ids"],
                             [first.id], label)
            # ids only — no name text, no birthdate, no registration number.
            self.assertNotIn("name", warns[0].detail, label)
            # Both records EXIST — warned, never blocked, never merged.
            self.assertEqual(len(store.all_players()), 2, label)

    def test_disambiguated_same_name_does_not_warn(self):
        for label, store, api, setup in self._each():
            setup.add_player("t1", None, Position.FORWARD, first_name="Alex",
                             last_name="Kim", birthdate="2015-01-01")
            setup.add_player("t1", None, Position.FORWARD, first_name="Alex",
                             last_name="Kim", birthdate="2016-02-02")
            self.assertEqual(self._warnings(store), [], label)

    def test_duplicate_report_three_shapes(self):
        for label, store, api, setup in self._each():
            a = setup.add_player("t1", "Sam Lee", Position.FORWARD)
            b = setup.add_player("t1", "Sam  lee", Position.GOALIE)
            c = setup.add_player(
                "t1", None, Position.FORWARD, first_name="Pat",
                last_name="Chen", birthdate="2014-05-05",
                registration_number="HC-77")
            d = setup.add_player(
                "t2", None, Position.FORWARD, first_name="Pat",
                last_name="Chen", birthdate="2014-05-05",
                registration_number="HC-77")
            report = setup.player_duplicate_report()
            by_type = {}
            for warning in report:
                by_type.setdefault(warning["type"], []).append(warning)
            same_name = by_type["same_name_same_team_undisambiguated"]
            self.assertEqual(same_name[0]["player_ids"],
                             sorted([a.id, b.id]), label)
            shared_reg = by_type["shared_registration_number"]
            self.assertEqual(shared_reg[0]["player_ids"],
                             sorted([c.id, d.id]), label)
            self.assertEqual(shared_reg[0]["registration_number"], "HC-77",
                             label)
            cross = by_type["same_name_same_birthdate"]
            self.assertEqual(cross[0]["player_ids"], sorted([c.id, d.id]),
                             label)
            # The cross-team hint never echoes the shared birthdate.
            self.assertNotIn("birthdate", cross[0], label)
            # Team narrowing: t2 only sees its own rows' groupings.
            t2_report = setup.player_duplicate_report(team_id="t2")
            self.assertEqual(
                {w["type"] for w in t2_report}, set(), label)


class UpdatePlayerIdentityTest(AthleteIdentityTestBase):
    def _structured(self, setup):
        return setup.add_player(
            "t1", None, Position.FORWARD, first_name="Jane",
            last_name="Smith", birthdate="2015-03-02",
            registration_number="HC-1", skill_rating=4, actor_id="op")

    def test_editing_part_re_derives_display_name(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            updated = setup.update_player(p.id, first_name="Janet",
                                          actor_id="op")
            self.assertEqual(updated.name, "Janet Smith", label)
            audit = [a for a in store.all_setup_audit()
                     if a.action == "player_updated"][-1]
            self.assertEqual(set(audit.detail["changed_fields"]),
                             {"first_name", "name"}, label)

    def test_direct_name_edit_refused_while_structured(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.update_player(p.id, name="Free Text")
            self.assertEqual(ctx.exception.details["reason"],
                             "name_is_derived", label)

    def test_clearing_both_parts_returns_to_legacy_form(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            updated = setup.update_player(
                p.id, first_name=None, last_name=None, name="Legacy Name",
                actor_id="op")
            self.assertIsNone(updated.first_name, label)
            self.assertIsNone(updated.last_name, label)
            self.assertEqual(updated.name, "Legacy Name", label)
            # And the display name is editable again afterwards.
            self.assertEqual(
                setup.update_player(p.id, name="Renamed").name, "Renamed",
                label)

    def test_clearing_only_one_part_refused(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.update_player(p.id, first_name=None)
            self.assertEqual(ctx.exception.details["reason"],
                             "structured_name_incomplete", label)

    def test_identity_field_edits_and_registration_uniqueness(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            setup.add_player("t1", None, Position.GOALIE, first_name="O",
                             last_name="Ther", registration_number="HC-2")
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.update_player(p.id, registration_number="HC-2")
            self.assertEqual(ctx.exception.details["reason"],
                             "duplicate_registration_number", label)
            updated = setup.update_player(
                p.id, birthdate="2015-04-04", registration_number="HC-9",
                skill_rating=None, preferred_name="JJ", actor_id="op")
            self.assertEqual(updated.birthdate, "2015-04-04", label)
            self.assertEqual(updated.registration_number, "HC-9", label)
            self.assertIsNone(updated.skill_rating, label)
            audit = [a for a in store.all_setup_audit()
                     if a.action == "player_updated"][-1]
            self.assertEqual(
                set(audit.detail["changed_fields"]),
                {"birthdate", "registration_number", "skill_rating",
                 "preferred_name"}, label)
            # Values NEVER enter the audit trail.
            dumped = json.dumps(audit.detail)
            self.assertNotIn("2015-04-04", dumped, label)
            self.assertNotIn("HC-9", dumped, label)

    def test_noop_identity_update_writes_no_audit(self):
        for label, store, api, setup in self._each():
            p = self._structured(setup)
            before = len([a for a in store.all_setup_audit()
                          if a.action == "player_updated"])
            setup.update_player(p.id, first_name="Jane", last_name="Smith",
                                birthdate="2015-03-02", skill_rating=4)
            after = len([a for a in store.all_setup_audit()
                         if a.action == "player_updated"])
            self.assertEqual(after, before, label)


class FacadePrivacyTest(AthleteIdentityTestBase):
    """AC[2]: birthdate + registration identifiers absent from default
    payloads; the operator opt-in is explicit, like include_email (#268)."""

    def _each_with_program(self):
        """Like ``_each()``, but ``t1`` belongs to a real Program a
        LEAGUE_ADMIN can be authorized into -- required for the #424
        audit-wiring identity gate's ALLOW path: ``list_players``'s
        pre-existing #369 Program-scoping gate (triggered whenever a
        ``role`` is supplied) resolves to zero Teams on a Program-less
        fixture regardless of privacy grants, which would make an ALLOW
        assertion here indistinguishable from a scoping accident."""
        for label, store in _service_backends():
            with self.subTest(backend=label):
                store.add_program(Program(id="pr", name="P"))
                store.add_team(Team(id="t1", name="Team One",
                                    program_id="pr"))
                api = ApiService(store)
                try:
                    yield label, store, api, api.setup
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_default_player_payloads_never_carry_private_fields(self):
        for label, store, api, setup in self._each():
            created = api.create_player(
                "t1", first_name="Jane", last_name="Smith",
                position="forward", birthdate="2015-03-02",
                registration_number="HC-1", skill_rating=6)
            self.assertIsNone(created.get("error"), (label, created))
            for row in (created,
                        api.update_player(created["id"], preferred_name="JJ"),
                        api.set_player_active(created["id"], False),
                        api.set_player_active(created["id"], True),
                        *api.list_players(team_id="t1")):
                self.assertNotIn("birthdate", row, label)
                self.assertNotIn("registration_number", row, label)
                # The dead legacy field stayed out of the contract (AC[5]).
                self.assertNotIn("guardian_person_id", row, label)
            # Non-private identity is present by default.
            row = api.list_players(team_id="t1")[0]
            self.assertEqual(row["first_name"], "Jane", label)
            self.assertEqual(row["preferred_name"], "JJ", label)
            self.assertEqual(row["skill_rating"], 6, label)

    def test_operator_opt_in_returns_private_fields(self):
        # #424 audit-wiring: include_identity now reaches the SAME #426
        # policy+audit boundary include_email already used (see
        # ApiService.list_players's BIRTHDATE/REGISTRATION_NUMBER gate,
        # right above the include_email block it mirrors) -- an authorized
        # role is required for BOTH opt-ins now, closing the gap this
        # test's comment used to describe as future work. Uses
        # _each_with_program (not _each) so the pre-existing #369 Program-
        # scoping gate a supplied `role` also triggers actually authorizes
        # `t1`, isolating this assertion to the privacy gate.
        for label, store, api, setup in self._each_with_program():
            api.create_player(
                "t1", first_name="Jane", last_name="Smith",
                position="forward", birthdate="2015-03-02",
                registration_number="HC-1", email="j@x.com")
            row = api.list_players(
                team_id="t1", include_identity=True, include_email=True,
                role=Role.LEAGUE_ADMIN, user_id="admin1")[0]
            self.assertEqual(row["birthdate"], "2015-03-02", label)
            self.assertEqual(row["registration_number"], "HC-1", label)
            self.assertEqual(row["email"], "j@x.com", label)

    def test_operator_opt_in_masks_identity_for_an_unauthorized_caller(self):
        """#424 audit-wiring: a bare call (no role/user_id) now masks
        BIRTHDATE/REGISTRATION_NUMBER exactly like include_email already
        masked CONTACT_DESTINATION -- the roster itself still comes back
        (mask, not refuse), with one durable DENIED audit row per masked
        category."""
        for label, store, api, setup in self._each():
            api.create_player(
                "t1", first_name="Jane", last_name="Smith",
                position="forward", birthdate="2015-03-02",
                registration_number="HC-1", email="j@x.com")
            row = api.list_players(team_id="t1", include_identity=True,
                                   include_email=True)[0]
            self.assertIsNone(row["birthdate"], label)
            self.assertIsNone(row["registration_number"], label)
            self.assertIsNone(row["email"], label)
            outcomes = sorted(
                (r.category.value, r.outcome)
                for r in store.list_data_access())
            self.assertEqual(outcomes, [
                ("birthdate", "denied"),
                ("contact_destination", "denied"),
                ("registration_number", "denied")], label)

    def test_facade_error_shape_for_bad_identity_fields(self):
        for label, store, api, setup in self._each():
            out = api.create_player("t1", first_name="A", last_name="B",
                                    position="forward",
                                    birthdate="2030-01-01")
            self.assertEqual(out["error"]["code"], "validation_error", label)
            self.assertEqual(out["error"]["details"]["field"], "birthdate",
                             label)
            self.assertEqual(api.list_players(team_id="t1"), [], label)

    def test_duplicate_report_facade_shape(self):
        for label, store, api, setup in self._each():
            api.create_player("t1", name="Sam Lee", position="forward")
            api.create_player("t1", name="Sam Lee", position="goalie")
            report = api.player_duplicate_report()
            self.assertEqual(
                report["warnings"][0]["type"],
                "same_name_same_team_undisambiguated", label)


if __name__ == "__main__":
    unittest.main()
