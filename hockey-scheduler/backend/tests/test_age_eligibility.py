"""Versioned age-tier/cutoff eligibility (#273 AC[1]).

The pure domain evaluator (no store, no clock — every date passed in), the
versioned per-LeagueSeason rule rows (immutable, audited, warn-first
enforcement), and the service/facade evaluation answering *"is athlete X
age-eligible for Season/League/Division Y under rule version Z"* with honest
indeterminates — on Memory, SQLite, and PostgreSQL.
"""

import os
import unittest
from datetime import date, datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, League, LeagueSeason, Position, Program, Role, Season, Team,
    age_on, evaluate_age_eligibility, normalize_age_tiers, normalize_cutoff,
    normalize_enforcement)
from hockey_scheduler.domain.errors import (
    NotFoundError, ValidationError)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc
TIERS = [{"code": "U10", "max_age": 10}, {"code": "U13", "max_age": 13},
         {"code": "SENIOR", "max_age": None}]


class DomainEvaluatorTest(unittest.TestCase):
    """Pure-function truth table — no store, no clock anywhere."""

    def _run(self, **overrides):
        kwargs = dict(birthdate="2017-01-15", tier_declared="U10",
                      cutoff_month=12, cutoff_day=31,
                      season_start=datetime(2026, 9, 1, tzinfo=UTC),
                      tiers=TIERS)
        kwargs.update(overrides)
        return evaluate_age_eligibility(**kwargs)

    def test_strictly_under_max_age_is_eligible(self):
        result = self._run()  # age 9 on 2026-12-31
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["age_at_cutoff"], 9)
        self.assertEqual(result["cutoff_date"], "2026-12-31")

    def test_exactly_max_age_on_cutoff_is_ineligible(self):
        result = self._run(birthdate="2016-12-31")  # age 10 on the cutoff
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reason"], "over_age")
        self.assertEqual(result["age_at_cutoff"], 10)

    def test_birthday_the_day_after_cutoff_still_counts_young(self):
        # Born 2017-01-01: on 2026-12-31 the 10th birthday has not happened.
        result = self._run(birthdate="2017-01-01")
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["age_at_cutoff"], 9)

    def test_leap_day_birthdate_ticks_on_march_first(self):
        self.assertEqual(age_on(date(2016, 2, 29), date(2026, 2, 28)), 9)
        self.assertEqual(age_on(date(2016, 2, 29), date(2026, 3, 1)), 10)

    def test_open_tier_is_eligible_even_without_birthdate(self):
        result = self._run(birthdate=None, tier_declared="senior")
        self.assertEqual(result["status"], "eligible")
        self.assertIsNone(result["max_age"])

    def test_tier_matching_is_trimmed_case_insensitive_never_fuzzy(self):
        self.assertEqual(self._run(tier_declared=" u10 ")["status"],
                         "eligible")
        result = self._run(tier_declared="U1O")  # typo: letter O
        self.assertEqual((result["status"], result["reason"]),
                         ("indeterminate", "unknown_tier"))

    def test_honest_indeterminates(self):
        cases = [
            (dict(birthdate=None), "no_birthdate"),
            (dict(tier_declared=""), "no_tier_declared"),
            (dict(tier_declared="U18"), "unknown_tier"),
            (dict(season_start=None), "no_season_start"),
            (dict(birthdate="not-a-date"), "invalid_birthdate"),
        ]
        for overrides, reason in cases:
            result = self._run(**overrides)
            self.assertEqual((result["status"], result["reason"]),
                             ("indeterminate", reason), overrides)

    def test_result_never_contains_the_birthdate(self):
        result = self._run()
        self.assertNotIn("birthdate", result)

    def test_normalizers(self):
        self.assertEqual(normalize_cutoff(2, 29), (None, "day_out_of_range"))
        self.assertEqual(normalize_cutoff(13, 1), (None, "month_out_of_range"))
        self.assertEqual(normalize_cutoff(8, 31), ((8, 31), None))
        tiers, reason = normalize_age_tiers(
            [{"code": " u10 ", "max_age": 10}])
        self.assertIsNone(reason)
        self.assertEqual(tiers, [{"code": "U10", "max_age": 10}])
        self.assertEqual(
            normalize_age_tiers([{"code": "U10", "max_age": 10},
                                 {"code": "u10", "max_age": 11}]),
            (None, "tier_code_duplicate"))
        self.assertEqual(normalize_age_tiers([{"code": "U10", "max_age": 0}]),
                         (None, "tier_max_age_invalid"))
        self.assertEqual(normalize_age_tiers([]), (None, "tiers_empty"))
        self.assertEqual(
            normalize_age_tiers([{"code": "U10", "extra": 1}]),
            (None, "tier_unknown_key"))
        self.assertEqual(normalize_enforcement(None), ("warn", None))
        self.assertEqual(normalize_enforcement(" BLOCK "), ("block", None))
        self.assertEqual(normalize_enforcement("maybe"),
                         (None, "invalid_enforcement"))


def _service_backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


class EligibilityRuleServiceTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                store.add_program(Program(id="pr", name="P"))
                store.add_season(Season(
                    id="se", program_id="pr", name="Fall",
                    start_date=datetime(2026, 9, 1, tzinfo=UTC)))
                store.add_league(League(id="lg", program_id="pr", name="L"))
                store.add_league_season(LeagueSeason(
                    id="ls", league_id="lg", season_id="se"))
                store.add_division(Division(
                    id="d", league_season_id="ls", name="D1",
                    age_group="U10"))
                store.add_team(Team(id="t1", name="T1"))
                api = ApiService(store)
                try:
                    yield label, store, api, api.setup
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_versions_append_and_round_trip(self):
        for label, store, api, setup in self._each():
            r1 = setup.set_age_eligibility_rule(
                "ls", 12, 31, TIERS, actor_id="op")
            r2 = setup.set_age_eligibility_rule(
                "ls", 8, 31, [{"code": "U10", "max_age": 10}],
                enforcement="block", actor_id="op")
            self.assertEqual((r1.version, r2.version), (1, 2), label)
            rules = store.age_eligibility_rules_for_league_season("ls")
            self.assertEqual([r.version for r in rules], [1, 2], label)
            # tiers survive the store round trip typed (JSON column on SQL).
            self.assertEqual(rules[0].tiers, [
                {"code": "U10", "max_age": 10},
                {"code": "U13", "max_age": 13},
                {"code": "SENIOR", "max_age": None}], label)
            self.assertEqual(rules[1].enforcement, "block", label)
            self.assertEqual(
                setup.current_age_eligibility_rule("ls").version, 2, label)
            audits = [a for a in store.all_setup_audit()
                      if a.action == "age_eligibility_rule_set"]
            self.assertEqual([a.detail["version"] for a in audits], [1, 2],
                             label)
            self.assertEqual(audits[0].detail["tier_codes"],
                             ["U10", "U13", "SENIOR"], label)

    def test_rule_validation_and_missing_league_season(self):
        for label, store, api, setup in self._each():
            with self.assertRaises(NotFoundError, msg=label):
                setup.set_age_eligibility_rule("nope", 12, 31, TIERS)
            for kwargs, reason in [
                    (dict(cutoff_month=2, cutoff_day=29), "invalid_cutoff"),
                    (dict(cutoff_month="12", cutoff_day=31),
                     "invalid_cutoff"),
                    (dict(tiers=[]), "invalid_tiers"),
                    (dict(tiers=[{"code": "", "max_age": 5}],),
                     "invalid_tiers"),
                    (dict(enforcement="sometimes"), "invalid_enforcement")]:
                base = dict(cutoff_month=12, cutoff_day=31, tiers=TIERS)
                base.update(kwargs)
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    setup.set_age_eligibility_rule("ls", **base)
                self.assertEqual(ctx.exception.details["reason"], reason,
                                 f"{label}: {kwargs}")
            self.assertEqual(
                store.age_eligibility_rules_for_league_season("ls"), [],
                label)

    def test_evaluation_pins_the_rule_version_it_used(self):
        for label, store, api, setup in self._each():
            player = setup.add_player(
                "t1", None, Position.FORWARD, first_name="Kid",
                last_name="Nine", birthdate="2017-01-15")
            no_rule = setup.evaluate_player_age_eligibility(player.id, "d")
            self.assertEqual((no_rule["status"], no_rule["reason"]),
                             ("indeterminate", "no_rule"), label)
            setup.set_age_eligibility_rule("ls", 12, 31, TIERS)
            eligible = setup.evaluate_player_age_eligibility(player.id, "d")
            self.assertEqual(eligible["status"], "eligible", label)
            self.assertEqual(eligible["rule_version"], 1, label)
            self.assertEqual(eligible["enforcement"], "warn", label)
            self.assertEqual(eligible["league_season_id"], "ls", label)
            # A new version changes the answer AND the pinned version:
            # same cutoff, cap lowered under this athlete's age (9).
            setup.set_age_eligibility_rule(
                "ls", 12, 31, [{"code": "U10", "max_age": 9}])
            now_over = setup.evaluate_player_age_eligibility(player.id, "d")
            self.assertEqual(now_over["rule_version"], 2, label)
            self.assertEqual(now_over["status"], "ineligible", label)
            self.assertEqual(now_over["age_at_cutoff"], 9, label)

    def test_no_birthdate_is_warned_never_guessed(self):
        for label, store, api, setup in self._each():
            setup.set_age_eligibility_rule("ls", 12, 31, TIERS)
            legacy = setup.add_player("t1", "No Birthdate", Position.GOALIE)
            result = setup.evaluate_player_age_eligibility(legacy.id, "d")
            self.assertEqual((result["status"], result["reason"]),
                             ("indeterminate", "no_birthdate"), label)

    def test_facade_summary_is_coach_safe_and_details_opt_in(self):
        for label, store, api, setup in self._each():
            setup.set_age_eligibility_rule("ls", 12, 31, TIERS)
            player = setup.add_player(
                "t1", None, Position.FORWARD, first_name="Kid",
                last_name="Nine", birthdate="2017-01-15")
            # #424 audit-wiring: the facade method is now gated on
            # visibility_policy.may_read_summary(role, BIRTHDATE) -- COACH
            # is exactly the role the bounded-#124 ruling grants SUMMARY
            # fidelity to for this category.
            summary = api.evaluate_player_eligibility(
                player.id, "d", actor_role=Role.COACH,
                actor_user_id="coach1")
            self.assertNotIn("error", summary, label)
            self.assertEqual(summary["status"], "eligible", label)
            self.assertEqual(summary["rule_version"], 1, label)
            self.assertNotIn("age_at_cutoff", summary, label)
            self.assertNotIn("cutoff_date", summary, label)
            self.assertNotIn("birthdate", summary, label)
            details = api.evaluate_player_eligibility(
                player.id, "d", include_details=True,
                actor_role=Role.COACH, actor_user_id="coach1")
            self.assertEqual(details["age_at_cutoff"], 9, label)
            self.assertEqual(details["cutoff_date"], "2026-12-31", label)
            self.assertNotIn("birthdate", details, label)
            rules = api.list_age_eligibility_rules("ls")
            self.assertEqual(rules[0]["version"], 1, label)
            self.assertEqual(rules[0]["tiers"][0], {"code": "U10",
                                                    "max_age": 10}, label)
            missing = api.evaluate_player_eligibility(
                "ghost", "d", actor_role=Role.COACH, actor_user_id="coach1")
            self.assertEqual(missing["error"]["code"], "not_found", label)

    def test_facade_refuses_an_unauthorized_caller_for_the_whole_call(self):
        """#424 audit-wiring: a bare/unauthorized caller gets the WHOLE
        call refused (no summary shape at all, not even the default
        status/reason -- it is itself derived from a BIRTHDATE read), with
        a durable DENIED audit row; an authorized ALLOW leaves exactly one
        ALLOWED row naming the player subject."""
        for label, store, api, setup in self._each():
            setup.set_age_eligibility_rule("ls", 12, 31, TIERS)
            player = setup.add_player(
                "t1", None, Position.FORWARD, first_name="Kid",
                last_name="Nine", birthdate="2017-01-15")
            denied = api.evaluate_player_eligibility(player.id, "d")
            self.assertEqual(denied["error"]["code"], "forbidden", label)
            rows = store.list_data_access()
            self.assertEqual(len(rows), 1, label)
            self.assertEqual(rows[0].category.value, "birthdate", label)
            self.assertEqual(rows[0].outcome, "denied", label)
            allowed = api.evaluate_player_eligibility(
                player.id, "d", actor_role=Role.LEAGUE_ADMIN,
                actor_user_id="admin1")
            self.assertNotIn("error", allowed, label)
            rows2 = store.list_data_access()
            self.assertEqual(len(rows2), 2, label)
            self.assertEqual(rows2[1].outcome, "allowed", label)
            self.assertEqual(rows2[1].subject_id, f"player:{player.id}",
                             label)


# =========================================================================== #
# #273 review round 2 finding 4: a birthdate AFTER the cutoff must never      #
# report eligible or a negative displayed age.                                #
# =========================================================================== #
class NotBornAtCutoffTest(unittest.TestCase):
    """cutoff 2010-12-31, birthdate 2015-01-01 (the reviewer's own example)
    used to return ``status="eligible"`` with ``age_at_cutoff=-5`` on a
    BOUNDED tier -- ``age_on()`` naively subtracts calendar years with no
    floor. An OPEN tier short-circuited even earlier: it reported eligible
    without ever looking at birthdate at all. Both are now a stable,
    distinct, non-eligible outcome (``not_born_at_cutoff``), and
    ``age_at_cutoff`` is never negative -- it stays ``None`` (there is no
    meaningful age yet), same as every other indeterminate/ineligible
    outcome that has nothing to report."""

    def _run(self, **overrides):
        kwargs = dict(birthdate="2017-01-15", tier_declared="U10",
                      cutoff_month=12, cutoff_day=31,
                      season_start=datetime(2026, 9, 1, tzinfo=UTC),
                      tiers=TIERS)
        kwargs.update(overrides)
        return evaluate_age_eligibility(**kwargs)

    def test_reviewers_own_example_bounded_tier(self):
        result = self._run(birthdate="2015-01-01", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2010, 6, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reason"], "not_born_at_cutoff")
        self.assertIsNone(result["age_at_cutoff"])  # never negative
        self.assertEqual(result["cutoff_date"], "2010-12-31")

    def test_open_tier_no_longer_short_circuits_before_birth(self):
        # SENIOR is max_age=None (open). Before the fix this returned
        # "eligible" immediately, never even looking at birthdate.
        result = self._run(birthdate="2015-01-01", tier_declared="senior",
                           cutoff_month=12, cutoff_day=31,
                           season_start=datetime(2010, 6, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reason"], "not_born_at_cutoff")
        self.assertIsNone(result["age_at_cutoff"])
        self.assertIsNone(result["max_age"])  # still names the open tier

    def test_open_tier_with_no_birthdate_is_still_eligible(self):
        """Positive control: the fix does not regress the legacy-athlete
        case an open tier is FOR -- no birthdate on file at all."""
        result = self._run(birthdate=None, tier_declared="senior")
        self.assertEqual(result["status"], "eligible")

    def test_birth_exactly_on_cutoff_is_normal_not_special_cased(self):
        # Born the SAME day as the cutoff: age 0 at cutoff, ordinarily
        # eligible for any bounded tier -- must NOT be treated as
        # not-yet-born (the boundary is strictly AFTER, never on).
        result = self._run(birthdate="2026-12-31", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["age_at_cutoff"], 0)
        self.assertIsNone(result["reason"])

    def test_birth_the_day_after_cutoff_is_not_born_at_cutoff(self):
        result = self._run(birthdate="2027-01-01", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reason"], "not_born_at_cutoff")
        self.assertIsNone(result["age_at_cutoff"])

    def test_bounded_tier_born_well_before_cutoff_is_unaffected(self):
        """Positive control: the ordinary case must not regress."""
        result = self._run(birthdate="2017-01-15", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["age_at_cutoff"], 9)
        self.assertIsNone(result["reason"])

    def test_historical_season_the_cutoff_year_is_still_the_seasons_own(self):
        # A Season from the PAST: the cutoff is anchored to ITS start year
        # (2010), not today's. A birthdate after THAT historical cutoff is
        # still not_born_at_cutoff -- the bug is about ordering birthdate
        # against cutoff, not about "the cutoff being in the future".
        result = self._run(birthdate="2011-06-01", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2010, 9, 1, tzinfo=UTC))
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reason"], "not_born_at_cutoff")
        self.assertEqual(result["cutoff_date"], "2010-12-31")
        # And a birthdate before that SAME historical cutoff is normal.
        normal = self._run(birthdate="2009-06-01", cutoff_month=12,
                           cutoff_day=31,
                           season_start=datetime(2010, 9, 1, tzinfo=UTC))
        self.assertEqual(normal["status"], "eligible")
        self.assertEqual(normal["age_at_cutoff"], 1)


class NotBornAtCutoffFacadeTest(unittest.TestCase):
    """The facade summary/detail shapes on Memory/SQLite/[PostgreSQL]: a
    not-yet-born athlete is reported through the SAME two shapes as every
    other eligibility answer, and ``age_at_cutoff``/``cutoff_date`` stay
    out of the Coach-facing summary exactly as they do for every other
    outcome (#124 boundary) -- this finding does not carve out a special
    exception."""

    def _each(self):
        # A HISTORICAL Season (starts 2015, cutoff -> 2015-12-31): a
        # perfectly ordinary, valid (non-future, relative to today)
        # birthdate can still land AFTER a season this old ran -- the
        # real-world shape of this bug, not a birthdate literally in the
        # future (which add_player's OWN, separate in_future guard already
        # refuses at write time, before any eligibility evaluation).
        for label, store in _service_backends():
            with self.subTest(backend=label):
                store.add_program(Program(id="pr", name="P"))
                store.add_season(Season(
                    id="se", program_id="pr", name="2015",
                    start_date=datetime(2015, 9, 1, tzinfo=UTC)))
                store.add_league(League(id="lg", program_id="pr", name="L"))
                store.add_league_season(LeagueSeason(
                    id="ls", league_id="lg", season_id="se"))
                store.add_division(Division(
                    id="d", league_season_id="ls", name="D1",
                    age_group="U10"))
                store.add_team(Team(id="t1", name="T1"))
                api = ApiService(store)
                try:
                    yield label, store, api, api.setup
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_not_yet_born_athlete_via_facade_summary_and_detail(self):
        for label, store, api, setup in self._each():
            setup.set_age_eligibility_rule("ls", 12, 31, TIERS)
            # Cutoff is 2015-12-31 (the 2015 Season's own start year); this
            # athlete's real, valid birthdate is after it.
            player = setup.add_player(
                "t1", None, Position.FORWARD, first_name="Not",
                last_name="YetBorn", birthdate="2020-03-01")
            detail = api.evaluate_player_eligibility(
                player.id, "d", include_details=True,
                actor_role=Role.LEAGUE_ADMIN, actor_user_id="admin1")
            self.assertEqual(detail["status"], "ineligible", label)
            self.assertEqual(detail["reason"], "not_born_at_cutoff", label)
            self.assertIsNone(detail["age_at_cutoff"], label)
            self.assertNotIn("birthdate", detail, label)
            summary = api.evaluate_player_eligibility(
                player.id, "d", actor_role=Role.COACH,
                actor_user_id="coach1")
            self.assertEqual(summary["status"], "ineligible", label)
            self.assertEqual(summary["reason"], "not_born_at_cutoff", label)
            self.assertNotIn("age_at_cutoff", summary, label)
            self.assertNotIn("cutoff_date", summary, label)
            self.assertNotIn("birthdate", summary, label)


if __name__ == "__main__":
    unittest.main()
