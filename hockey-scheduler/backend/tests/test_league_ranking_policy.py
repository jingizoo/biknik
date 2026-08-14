"""LeagueRankingPolicy persistence + CRUD (#287 slice 1, migration 054).

WHAT THIS IS. The per-League substitute-matching CONFIGURATION issue #287
decided a League may own: the ordered rule list (fairness, skill_proximity,
position_preference, random — each with an enabled flag, order = priority,
every kind exactly once so disabling is explicit and omission is
unrepresentable), the notice-window exclusion flag, the random seed, and the
configurable offer-response deadline. One row per League (054's unique
index); an unconfigured League resolves to the well-defined in-code default
(issue order, all enabled, notice window on, seed 0, 24 h) marked
``source: "default"`` with ``id: None`` — a default is not a stored row and
never masquerades as one.

WHAT THIS IS NOT. No consumer: nothing on this branch feeds a ranking
computation — the deterministic ranking engine is a separate in-flight PR,
and wiring policy + engine into the substitute candidate list is the next
#287 slice, deferred until that engine merges. The goalie/skater separation
is deliberately NOT configurable data (hard gate), and the notice window is
an exclusion flag, not an ordering rule — both pinned here so a future edit
cannot quietly turn either into reorderable policy. #287's five open owner
questions are not encoded.

TRI-STORE. Memory + SQLite in every contract test; PostgreSQL runs the
stored-and-reloaded round-trip. A SKIP IS NOT A PASS — the PostgreSQL class
announces loudly when TEST_DATABASE_URL is unset.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import fresh_sql_store

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    RankingRuleKind,
    default_league_ranking_rules,
)
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = "setup_admin"

_DEFAULT_RULES = [
    {"kind": "fairness", "enabled": True},
    {"kind": "skill_proximity", "enabled": True},
    {"kind": "position_preference", "enabled": True},
    {"kind": "random", "enabled": True},
]


def _fixture(api):
    org = api.create_organization("Org", "O", actor_id=ADMIN)
    program = api.create_program("Prog", operator_organization_id=org["id"],
                                 actor_id=ADMIN)
    season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
    league = api.create_league(season["id"], "Elite", actor_id=ADMIN)
    return league


class _Contract:
    def _each(self):
        for label, store in (("memory", InMemoryStore()),
                             ("sqlite", SqlStore(":memory:"))):
            api = ApiService(store)
            yield label, api, _fixture(api)


class DefaultPolicyTest(_Contract, unittest.TestCase):
    def test_unconfigured_league_resolves_to_the_documented_default(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                got = api.get_league_ranking_policy(league["id"])
                self.assertEqual(got, {
                    "id": None, "league_id": league["id"],
                    "rules": _DEFAULT_RULES,
                    "notice_window_enabled": True,
                    "random_seed": 0,
                    "offer_response_deadline_minutes": 1440,
                    "source": "default",
                }, label)
                # And nothing was persisted by reading.
                self.assertEqual(
                    api.store.all_league_ranking_policies(), [], label)

    def test_default_rules_cover_every_kind_exactly_once_in_issue_order(self):
        self.assertEqual(default_league_ranking_rules(), _DEFAULT_RULES)
        self.assertEqual([r["kind"] for r in _DEFAULT_RULES],
                         [k.value for k in RankingRuleKind])
        # A fresh list every call — no shared mutable default.
        a, b = default_league_ranking_rules(), default_league_ranking_rules()
        a[0]["enabled"] = False
        self.assertTrue(b[0]["enabled"])

    def test_unknown_league_is_not_found_never_a_default(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                got = api.get_league_ranking_policy("nope")
                self.assertEqual(got["error"]["code"], "not_found", label)


class SetPolicyTest(_Contract, unittest.TestCase):
    def test_full_set_persists_and_reads_back_as_league_source(self):
        reordered = [
            {"kind": "position_preference", "enabled": True},
            {"kind": "skill_proximity", "enabled": False},
            {"kind": "fairness", "enabled": True},
            {"kind": "random", "enabled": True},
        ]
        for label, api, league in self._each():
            with self.subTest(backend=label):
                before = len(api.store.all_setup_audit())
                got = api.set_league_ranking_policy(
                    league["id"], rules=reordered,
                    notice_window_enabled=False, random_seed=42,
                    offer_response_deadline_minutes=120, actor_id=ADMIN)
                self.assertNotIn("error", got, (label, got))
                self.assertEqual(got["source"], "league", label)
                self.assertEqual(got["rules"], reordered, label)
                self.assertFalse(got["notice_window_enabled"], label)
                self.assertEqual(
                    (got["random_seed"],
                     got["offer_response_deadline_minutes"]),
                    (42, 120), label)
                again = api.get_league_ranking_policy(league["id"])
                self.assertEqual(again["rules"], reordered, label)
                self.assertEqual(again["source"], "league", label)
                audits = api.store.all_setup_audit()[before:]
                self.assertIn("league_ranking_policy_set",
                              [a.action for a in audits], label)

    def test_partial_set_keeps_the_other_effective_values(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                # First-time partial set starts from the DEFAULT, not zeroes.
                got = api.set_league_ranking_policy(
                    league["id"], random_seed=7, actor_id=ADMIN)
                self.assertEqual(got["random_seed"], 7, label)
                self.assertEqual(got["rules"], _DEFAULT_RULES, label)
                self.assertEqual(got["offer_response_deadline_minutes"],
                                 1440, label)
                # Second partial set keeps the stored seed.
                got = api.set_league_ranking_policy(
                    league["id"], offer_response_deadline_minutes=60,
                    actor_id=ADMIN)
                self.assertEqual(
                    (got["random_seed"],
                     got["offer_response_deadline_minutes"]),
                    (7, 60), label)
                # One row per League throughout (054's unique index scope).
                self.assertEqual(
                    len(api.store.all_league_ranking_policies()), 1, label)

    def test_reset_returns_to_default_and_audits_only_real_removals(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                # Resetting an unconfigured League: no-op, no audit.
                before = len(api.store.all_setup_audit())
                got = api.reset_league_ranking_policy(league["id"],
                                                      actor_id=ADMIN)
                self.assertEqual((got["reset"], got["source"]),
                                 (False, "default"), label)
                self.assertEqual(len(api.store.all_setup_audit()), before,
                                 label)
                api.set_league_ranking_policy(league["id"], random_seed=1,
                                              actor_id=ADMIN)
                got = api.reset_league_ranking_policy(league["id"],
                                                      actor_id=ADMIN)
                self.assertEqual((got["reset"], got["source"]),
                                 (True, "default"), label)
                self.assertEqual(
                    api.store.all_league_ranking_policies(), [], label)
                self.assertIn("league_ranking_policy_reset",
                              [a.action for a in api.store.all_setup_audit()],
                              label)


class ValidationTest(_Contract, unittest.TestCase):
    def _rejects(self, api, league, reason, label, **kwargs):
        before = api.get_league_ranking_policy(league["id"])
        got = api.set_league_ranking_policy(league["id"], actor_id=ADMIN,
                                            **kwargs)
        self.assertIn("error", got, (label, reason, got))
        self.assertEqual(got["error"]["details"]["reason"], reason,
                         (label, got))
        # Zero mutation on rejection.
        self.assertEqual(api.get_league_ranking_policy(league["id"]),
                         before, (label, reason))

    def test_rules_validation_is_exhaustive(self):
        three = [r for r in _DEFAULT_RULES if r["kind"] != "random"]
        for label, api, league in self._each():
            with self.subTest(backend=label):
                self._rejects(api, league, "invalid_ranking_rules", label,
                              rules="fairness-first")
                self._rejects(api, league, "invalid_ranking_rule_entry",
                              label, rules=_DEFAULT_RULES[:3] + ["random"])
                self._rejects(api, league, "invalid_ranking_rule_entry",
                              label,
                              rules=three + [{"kind": "random",
                                              "enabled": True,
                                              "weight": 3}])
                self._rejects(api, league, "unknown_ranking_rule_kind", label,
                              rules=three + [{"kind": "goalie_gate",
                                              "enabled": True}])
                self._rejects(api, league, "duplicate_ranking_rule_kind",
                              label,
                              rules=_DEFAULT_RULES + [{"kind": "fairness",
                                                       "enabled": False}])
                self._rejects(api, league, "missing_ranking_rule_kinds",
                              label, rules=three)
                self._rejects(api, league, "invalid_ranking_rule_enabled",
                              label,
                              rules=three + [{"kind": "random",
                                              "enabled": "yes"}])

    def test_flag_seed_and_deadline_validation(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                self._rejects(api, league, "invalid_notice_window_enabled",
                              label, notice_window_enabled="on")
                self._rejects(api, league, "invalid_random_seed", label,
                              random_seed="7")
                self._rejects(api, league, "invalid_random_seed", label,
                              random_seed=True)
                self._rejects(api, league,
                              "invalid_offer_response_deadline_minutes",
                              label, offer_response_deadline_minutes=0)
                self._rejects(api, league,
                              "invalid_offer_response_deadline_minutes",
                              label, offer_response_deadline_minutes=None)

    def test_set_on_unknown_league_is_not_found_zero_mutation(self):
        for label, api, league in self._each():
            with self.subTest(backend=label):
                got = api.set_league_ranking_policy("nope", random_seed=1,
                                                    actor_id=ADMIN)
                self.assertEqual(got["error"]["code"], "not_found", label)
                self.assertEqual(
                    api.store.all_league_ranking_policies(), [], label)


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #287 slice-1 LeagueRankingPolicy persistence was "
            "NOT exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: "
            "migration 054's DDL, the unique-per-League index and the "
            "JSON/bool/int round-trips only prove out on a real engine. Set "
            "TEST_DATABASE_URL (run_parallel.py --postgres does) to run it.")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class RankingPolicyPostgresTest(unittest.TestCase):
    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        self.api = ApiService(self.store)
        self.league = _fixture(self.api)

    def test_round_trip_and_durability_on_postgres(self):
        api, league = self.api, self.league
        reordered = list(reversed(default_league_ranking_rules()))
        got = api.set_league_ranking_policy(
            league["id"], rules=reordered, notice_window_enabled=False,
            random_seed=13, offer_response_deadline_minutes=45,
            actor_id=ADMIN)
        self.assertNotIn("error", got, got)
        # Durable across a second connection to the same database.
        second = SqlStore(os.environ["TEST_DATABASE_URL"])
        try:
            row = second.ranking_policy_for_league(league["id"])
            self.assertEqual(row.rules, reordered)
            self.assertIs(row.notice_window_enabled, False)
            self.assertEqual((row.random_seed,
                              row.offer_response_deadline_minutes), (13, 45))
        finally:
            second.close()
        self.assertTrue(
            api.reset_league_ranking_policy(league["id"],
                                            actor_id=ADMIN)["reset"])
        self.assertIsNone(
            self.store.ranking_policy_for_league(league["id"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
