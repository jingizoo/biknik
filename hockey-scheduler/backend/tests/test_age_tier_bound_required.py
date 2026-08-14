"""normalize_age_tiers() must require the ``max_age`` KEY, not merely a
non-null value (#273 review round 3 finding 2).

``{"code": "U10"}`` (the ``max_age`` key entirely omitted) used to canonicalize
identically to ``{"code": "U10", "max_age": None}`` (the key present,
EXPLICITLY null) — both landed as an open (unbounded) tier via a bare
``tier.get("max_age")``. Missing data must never silently weaken an age
policy: an operator who forgets a column, or an upstream integration that
drops a null-valued key entirely (a common JSON-serialization choice), gets
an accidental Senior-style open tier under a bounded tier's own code name —
reported eligible in this repo's own reproduction: a 26-year-old evaluated
as eligible for "U10" on both Memory and SQLite, purely because the bound
was never supplied.

The fix requires the key to be present on every tier
(``tier_max_age_missing`` when it is not — a different, harder reason than
the existing ``tier_max_age_invalid`` for a present-but-malformed value);
only an EXPLICIT ``null`` still declares an intentional open tier, exactly
as before.

Covers, on Memory/SQLite/[PostgreSQL]:

* the pure domain function's omitted-vs-explicit-null distinction, integer
  boundaries (1 and 99), and multi-tier / mixed-error ordering;
* service (``SetupService.set_age_eligibility_rule``) AND facade
  (``ApiService.set_age_eligibility_rule``) rejection of both an omitted key
  and a malformed present value, each with ZERO rule rows and ZERO audit
  entries written;
* an explicit-null open tier still succeeds end-to-end: service write,
  store round-trip (both the raw ``AgeEligibilityRule.tiers`` and the
  facade's ``list_age_eligibility_rules`` JSON shape), and a real
  evaluation reporting an elderly (or any) athlete eligible;
* a bounded tier at each integer boundary round-trips and evaluates
  correctly too, as a positive control against over-tightening.
"""

import os
import unittest
from datetime import timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Division, Position, Team, normalize_age_tiers
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


# =========================================================================== #
# Pure domain function: no store, no clock.                                   #
# =========================================================================== #
class NormalizeAgeTiersUnitTest(unittest.TestCase):

    def test_omitted_max_age_key_is_rejected(self):
        self.assertEqual(normalize_age_tiers([{"code": "U10"}]),
                         (None, "tier_max_age_missing"))

    def test_explicit_null_max_age_is_still_accepted_open_tier(self):
        tiers, reason = normalize_age_tiers([{"code": "U10", "max_age": None}])
        self.assertIsNone(reason)
        self.assertEqual(tiers, [{"code": "U10", "max_age": None}])

    def test_omitted_key_and_explicit_null_resolve_differently(self):
        # Identical code, identical intent-shape otherwise -- the ONLY
        # difference is whether the key exists at all.
        omitted = normalize_age_tiers([{"code": "SENIOR"}])
        explicit = normalize_age_tiers([{"code": "SENIOR", "max_age": None}])
        self.assertEqual(omitted, (None, "tier_max_age_missing"))
        self.assertEqual(explicit,
                         ([{"code": "SENIOR", "max_age": None}], None))

    def test_min_and_max_integer_boundaries_accepted(self):
        self.assertEqual(normalize_age_tiers([{"code": "A", "max_age": 1}]),
                         ([{"code": "A", "max_age": 1}], None))
        self.assertEqual(normalize_age_tiers([{"code": "A", "max_age": 99}]),
                         ([{"code": "A", "max_age": 99}], None))

    def test_just_outside_boundaries_still_tier_max_age_invalid(self):
        self.assertEqual(normalize_age_tiers([{"code": "A", "max_age": 0}]),
                         (None, "tier_max_age_invalid"))
        self.assertEqual(normalize_age_tiers([{"code": "A", "max_age": 100}]),
                         (None, "tier_max_age_invalid"))

    def test_malformed_present_value_stays_the_existing_distinct_reason(self):
        # "malformed" (key present, bad value) and "absent" (key missing)
        # are deliberately DIFFERENT diagnostics -- never collapsed.
        for bad in ("10", 10.5, True, [10], {}):
            with self.subTest(bad=bad):
                self.assertEqual(
                    normalize_age_tiers([{"code": "U10", "max_age": bad}]),
                    (None, "tier_max_age_invalid"))

    def test_a_later_tiers_omitted_key_rejects_the_whole_list(self):
        # An earlier, perfectly valid tier does not hide a later omission --
        # the whole list is atomically rejected, never partially accepted.
        result = normalize_age_tiers(
            [{"code": "U10", "max_age": 10}, {"code": "U13"}])
        self.assertEqual(result, (None, "tier_max_age_missing"))

    def test_unknown_key_check_still_precedes_missing_max_age(self):
        # Pinned, unchanged ordering from before this finding (also covered
        # in test_age_eligibility.py's own normalizer test): an unrecognized
        # extra key is reported before the missing max_age key is even
        # considered.
        self.assertEqual(
            normalize_age_tiers([{"code": "U10", "extra": 1}]),
            (None, "tier_unknown_key"))

    def test_positive_control_a_normal_bounded_and_open_mix_still_works(self):
        # Nothing about the fix touches the ordinary, fully-specified case.
        tiers, reason = normalize_age_tiers(
            [{"code": "U10", "max_age": 10}, {"code": "SENIOR", "max_age": None}])
        self.assertIsNone(reason)
        self.assertEqual(tiers, [{"code": "U10", "max_age": 10},
                                 {"code": "SENIOR", "max_age": None}])


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _seed(store, season_name="S1", start_date=None):
    api = ApiService(store)
    program = api.create_program("Prog", "US", "UTC")
    season = api.create_season(program["id"], season_name,
                               start_date=start_date)
    api.create_league(season["id"], "Gold")
    ls_id = next(ls.id for ls in store.all_league_seasons()
                if ls.season_id == season["id"])
    return api, season["id"], ls_id


def _rule_audits(store, ls_id=None):
    rows = [a for a in store.all_setup_audit()
           if a.action == "age_eligibility_rule_set"]
    if ls_id is not None:
        rows = [a for a in rows if a.detail.get("league_season_id") == ls_id]
    return rows


# =========================================================================== #
# Service + facade rejection, zero rule rows, zero audits.                    #
# =========================================================================== #
class ServiceAndFacadeRejectionZeroWritesTest(unittest.TestCase):

    def test_service_rejects_omitted_key_zero_rule_zero_audit(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    api.setup.set_age_eligibility_rule(
                        ls_id, 12, 31, [{"code": "U10"}], actor_id="op")
                self.assertEqual(ctx.exception.details["reason"],
                                 "invalid_tiers", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_service_rejects_malformed_present_value_zero_rule_zero_audit(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    api.setup.set_age_eligibility_rule(
                        ls_id, 12, 31, [{"code": "U10", "max_age": "ten"}],
                        actor_id="op")
                self.assertEqual(ctx.exception.details["reason"],
                                 "invalid_tiers", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_service_rejects_second_tiers_omitted_key_zero_rule_zero_audit(self):
        # A valid FIRST tier must not smuggle a later omission through.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    api.setup.set_age_eligibility_rule(
                        ls_id, 12, 31,
                        [{"code": "U10", "max_age": 10}, {"code": "U13"}],
                        actor_id="op")
                self.assertEqual(ctx.exception.details["reason"],
                                 "invalid_tiers", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_facade_rejects_omitted_key_zero_rule_zero_audit(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                res = api.set_age_eligibility_rule(
                    ls_id, 12, 31, [{"code": "U10"}], actor_id="op")
                self.assertEqual(res["error"]["details"]["reason"],
                                 "invalid_tiers", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_facade_rejects_malformed_present_value_zero_rule_zero_audit(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                res = api.set_age_eligibility_rule(
                    ls_id, 12, 31, [{"code": "U10", "max_age": [10]}],
                    actor_id="op")
                self.assertEqual(res["error"]["details"]["reason"],
                                 "invalid_tiers", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Explicit-null open tier: still succeeds end-to-end.                         #
# =========================================================================== #
class ExplicitNullOpenTierSuccessTest(unittest.TestCase):

    def _seed_with_division(self, store, age_group):
        api, season_id, ls_id = _seed(store, start_date="2026-09-01")
        store.add_division(Division(
            id="d", league_season_id=ls_id, name="D1", age_group=age_group))
        store.add_team(Team(id="t1", name="T1"))
        return api, ls_id

    def test_explicit_null_persists_round_trips_and_evaluates_open(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, ls_id = self._seed_with_division(store, "senior")
                rule = api.setup.set_age_eligibility_rule(
                    ls_id, 12, 31, [{"code": "SENIOR", "max_age": None}],
                    actor_id="op")
                self.assertEqual(rule.tiers,
                                 [{"code": "SENIOR", "max_age": None}], label)
                # Round-trip through the store, a fresh read.
                persisted = store.age_eligibility_rules_for_league_season(ls_id)
                self.assertEqual(persisted[0].tiers,
                                 [{"code": "SENIOR", "max_age": None}], label)
                # Round-trip through the facade's own JSON-safe shape.
                via_facade = api.list_age_eligibility_rules(ls_id)
                self.assertEqual(via_facade[0]["tiers"],
                                 [{"code": "SENIOR", "max_age": None}], label)
                # A genuinely elderly athlete is reported eligible under the
                # EXPLICIT open tier -- the whole point of allowing it.
                elder = api.setup.add_player(
                    "t1", None, Position.FORWARD, first_name="Old",
                    last_name="Timer", birthdate="1950-01-01")
                result = api.setup.evaluate_player_age_eligibility(
                    elder.id, "d")
                self.assertEqual(result["status"], "eligible", label)
                self.assertIsNone(result["max_age"], label)
                # Coach-safe facade summary agrees.
                summary = api.evaluate_player_eligibility(elder.id, "d")
                self.assertEqual(summary["status"], "eligible", label)
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Bounded tier at each integer boundary: positive control, round trip.        #
# =========================================================================== #
class BoundedTierBoundaryPositiveControlTest(unittest.TestCase):

    def test_boundary_max_age_values_round_trip_and_evaluate(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store, start_date="2026-09-01")
                store.add_division(Division(
                    id="d1", league_season_id=ls_id, name="Min", age_group="MIN1"))
                store.add_division(Division(
                    id="d99", league_season_id=ls_id, name="Max", age_group="MAX99"))
                store.add_team(Team(id="t1", name="T1"))
                api.setup.set_age_eligibility_rule(
                    ls_id, 12, 31,
                    [{"code": "MIN1", "max_age": 1},
                     {"code": "MAX99", "max_age": 99}],
                    actor_id="op")
                rules = store.age_eligibility_rules_for_league_season(ls_id)
                self.assertEqual(
                    rules[0].tiers,
                    [{"code": "MIN1", "max_age": 1},
                     {"code": "MAX99", "max_age": 99}], label)
                infant = api.setup.add_player(
                    "t1", None, Position.FORWARD, first_name="Baby",
                    last_name="One", birthdate="2026-01-01")
                result = api.setup.evaluate_player_age_eligibility(
                    infant.id, "d1")
                self.assertEqual(result["status"], "eligible", label)
                self.assertEqual(result["age_at_cutoff"], 0, label)
                nonagenarian = api.setup.add_player(
                    "t1", None, Position.FORWARD, first_name="Elder",
                    last_name="Ninety", birthdate="1930-01-01")
                result99 = api.setup.evaluate_player_age_eligibility(
                    nonagenarian.id, "d99")
                self.assertEqual(result99["status"], "eligible", label)
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
