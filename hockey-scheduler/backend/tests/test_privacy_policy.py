"""Visibility-policy matrix units (#124).

Pins the FULL role x category matrix of ``services/visibility_policy.py`` —
every known role against every known category, plus the fail-closed defaults
for unknown/missing principals — so a policy regression is a one-line diff
here, never a silent widening. Server-level enforcement tests belong to the
post-#159 wiring follow-up; these are the policy's own ground truth.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.domain.privacy import (
    ACCESS_ALLOWED,
    ACCESS_DENIED,
    DataAccessLog,
    SensitiveFieldCategory,
)
from hockey_scheduler.services import visibility_policy as vp

C = SensitiveFieldCategory
L = vp.AccessLevel

# The complete expected matrix — every (role, category) pair, spelled out so
# the intent of #124's owner ruling is READ here, not derived: League Admin
# raw for identity/contact categories but NOTHING for medical/discipline;
# Arena Manager contact-only; Coach summary-only for the two eligibility
# inputs; everyone else nothing.
EXPECTED = {
    Role.LEAGUE_ADMIN: {
        C.BIRTHDATE: L.RAW,
        C.REGISTRATION_NUMBER: L.RAW,
        C.CONTACT_DESTINATION: L.RAW,
        C.EMERGENCY_CONTACT: L.RAW,
        C.MEDICAL_ALERT: L.NONE,
        C.DISCIPLINE_NOTE: L.NONE,
    },
    Role.ARENA_MANAGER: {
        C.BIRTHDATE: L.NONE,
        C.REGISTRATION_NUMBER: L.NONE,
        C.CONTACT_DESTINATION: L.RAW,
        C.EMERGENCY_CONTACT: L.NONE,
        C.MEDICAL_ALERT: L.NONE,
        C.DISCIPLINE_NOTE: L.NONE,
    },
    Role.COACH: {
        C.BIRTHDATE: L.SUMMARY,
        C.REGISTRATION_NUMBER: L.SUMMARY,
        C.CONTACT_DESTINATION: L.NONE,
        C.EMERGENCY_CONTACT: L.NONE,
        C.MEDICAL_ALERT: L.NONE,
        C.DISCIPLINE_NOTE: L.NONE,
    },
    Role.PLAYER: {c: L.NONE for c in C},
    Role.GUARDIAN: {c: L.NONE for c in C},
    Role.OFFICIAL: {c: L.NONE for c in C},
    Role.VIEWER: {c: L.NONE for c in C},
}


class PolicyMatrixTest(unittest.TestCase):
    def test_matrix_is_exhaustive_and_exact(self):
        # Every known role and every known category is asserted — no sampling.
        self.assertEqual(set(EXPECTED), set(Role))
        for role in Role:
            for category in C:
                self.assertEqual(
                    vp.access_level(role, category), EXPECTED[role][category],
                    f"{role.value} x {category.value}")

    def test_raw_and_summary_predicates_follow_the_matrix(self):
        for role in Role:
            for category in C:
                lvl = EXPECTED[role][category]
                self.assertEqual(vp.may_read_raw(role, category), lvl >= L.RAW,
                                 f"{role.value} x {category.value}")
                self.assertEqual(vp.may_read_summary(role, category),
                                 lvl >= L.SUMMARY,
                                 f"{role.value} x {category.value}")

    def test_raw_categories_projection(self):
        self.assertEqual(
            vp.raw_categories(Role.LEAGUE_ADMIN),
            frozenset({C.BIRTHDATE, C.REGISTRATION_NUMBER,
                       C.CONTACT_DESTINATION, C.EMERGENCY_CONTACT}))
        self.assertEqual(vp.raw_categories(Role.ARENA_MANAGER),
                         frozenset({C.CONTACT_DESTINATION}))
        for role in (Role.COACH, Role.PLAYER, Role.GUARDIAN,
                     Role.OFFICIAL, Role.VIEWER):
            self.assertEqual(vp.raw_categories(role), frozenset(), role.value)

    # -- the two named owner rulings, as their own tests -------------------
    def test_league_admin_is_not_medical_or_discipline_staff(self):
        for category in (C.MEDICAL_ALERT, C.DISCIPLINE_NOTE):
            self.assertFalse(vp.may_read_raw(Role.LEAGUE_ADMIN, category))
            # Not even a summary: "has a medical alert: yes" is still
            # health information. #274 defines who is authorized.
            self.assertFalse(vp.may_read_summary(Role.LEAGUE_ADMIN, category))

    def test_coach_tops_out_at_eligibility_summary(self):
        self.assertTrue(vp.may_read_summary(Role.COACH, C.BIRTHDATE))
        self.assertTrue(vp.may_read_summary(Role.COACH, C.REGISTRATION_NUMBER))
        for category in C:
            self.assertFalse(vp.may_read_raw(Role.COACH, category),
                             category.value)

    # -- fail-closed defaults ----------------------------------------------
    def test_unknown_or_missing_principal_gets_nothing(self):
        # #426 review finding 1: OPERATOR_BOUNDARY is retired — a missing
        # principal (None) now fails closed exactly like any other
        # unrecognised one, with no special-case grant anywhere.
        for principal in (None, "public", "root", "", "LEAGUE_ADMIN",
                          vp.NO_PRINCIPAL):
            for category in C:
                self.assertEqual(vp.access_level(principal, category), L.NONE,
                                 f"{principal!r} x {category.value}")
                self.assertFalse(vp.may_read_raw(principal, category))
                self.assertFalse(vp.may_read_summary(principal, category))
            self.assertEqual(vp.raw_categories(principal), frozenset())

    def test_unknown_category_gets_nothing_even_for_league_admin(self):
        self.assertEqual(
            vp.access_level(Role.LEAGUE_ADMIN, "future_category"), L.NONE)
        self.assertFalse(vp.may_read_raw(Role.LEAGUE_ADMIN, "future_category"))

    def test_no_principal_is_not_a_role(self):
        self.assertEqual(vp.NO_PRINCIPAL, "no_principal")
        self.assertNotIn(vp.NO_PRINCIPAL, [r.value for r in Role])

    # -- SYSTEM_PRINCIPAL (#426 review finding 2) ---------------------------
    def test_system_principal_is_raw_on_exactly_contact_destination(self):
        for category in C:
            expected = (L.RAW if category == C.CONTACT_DESTINATION
                       else L.NONE)
            self.assertEqual(
                vp.access_level(vp.SYSTEM_PRINCIPAL, category), expected,
                category.value)
        self.assertTrue(
            vp.may_read_raw(vp.SYSTEM_PRINCIPAL, C.CONTACT_DESTINATION))
        self.assertEqual(vp.SYSTEM_ATTESTED_CATEGORIES,
                         frozenset({C.CONTACT_DESTINATION}))

    def test_system_principal_is_not_reachable_by_value(self):
        # Identity-only sentinel: no string an HTTP caller could ever
        # supply (the label it prints as, its class name, a plain "system"
        # claim) may substitute for the real object and gain SYSTEM's
        # grant. NOTE: a role's OWN string value (e.g. "league_admin") is
        # a DIFFERENT, legitimate case — Role is a (str, Enum), so it
        # correctly resolves to that role's OWN grant, not SYSTEM's; that
        # is exercised by test_matrix_is_exhaustive_and_exact, not here.
        for spoof in ("system", "SYSTEM_PRINCIPAL", "SYSTEM", None, ""):
            self.assertFalse(
                vp.may_read_raw(spoof, C.CONTACT_DESTINATION), repr(spoof))

    def test_system_principal_is_a_singleton_not_a_role(self):
        self.assertNotIsInstance(vp.SYSTEM_PRINCIPAL, Role)
        self.assertNotIn(vp.SYSTEM_PRINCIPAL, list(Role))


class VocabularyTest(unittest.TestCase):
    """The category vocabulary is written into durable audit rows and is the
    string set the #202 RouteSpec sensitive-data classification can adopt —
    so its exact values are pinned."""

    def test_category_values_are_pinned(self):
        self.assertEqual(
            {c.value for c in C},
            {"birthdate", "registration_number", "contact_destination",
             "emergency_contact", "medical_alert", "discipline_note"})

    def test_outcome_constants_are_pinned(self):
        self.assertEqual(ACCESS_ALLOWED, "allowed")
        self.assertEqual(ACCESS_DENIED, "denied")

    def test_access_levels_are_strictly_ordered(self):
        self.assertLess(L.NONE, L.SUMMARY)
        self.assertLess(L.SUMMARY, L.RAW)

    def test_data_access_log_is_structurally_value_free(self):
        # The schema itself is the guarantee that no protected value can be
        # copied into the log: exactly these fields, and none that could
        # carry a stored value (no destination/value/detail field). `seq`
        # (#426 review finding 5) is the persisted ordering key, always
        # store-assigned — never a value the caller supplies.
        from dataclasses import fields
        self.assertEqual(
            [f.name for f in fields(DataAccessLog)],
            ["id", "category", "subject_type", "subject_id", "purpose", "at",
             "actor_user_id", "actor_role", "outcome", "request_id", "seq"])


if __name__ == "__main__":
    unittest.main()
