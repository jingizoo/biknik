"""Tests for the substitute ranking prototype (issue #287, slice 2).

Run: python3 -m unittest discover -s tests -p "test_substitute_ranking.py"
"""

import itertools
import unittest
from datetime import datetime, timedelta, timezone

from hockey_scheduler.domain.enums import Position, SlotType
from hockey_scheduler.services.substitute_ranking import (
    Exclusion,
    GateKind,
    LeagueRankingPolicy,
    MAX_SKILL,
    MIN_SKILL,
    OverrideRefusalKind,
    PolicyError,
    RuleKind,
    RuleSetting,
    SelectionOverride,
    SubstituteCandidate,
    SubstituteRequest,
    rank_substitutes,
)

GAME_START = datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)
AS_OF = GAME_START - timedelta(minutes=600)  # 10 hours of notice
OUT_OF_RANGE_SKILLS = (MIN_SKILL - 1, MAX_SKILL + 1, MIN_SKILL - 2)


def make_request(
    needed_position=Position.FORWARD,
    absent_skill=4,
    game_start=GAME_START,
    as_of=AS_OF,
    request_id="sub_req_1",
):
    """A vacancy names its position and nothing else — ``slot_type`` is derived,
    so there is no second argument that could contradict the first."""
    return SubstituteRequest(
        request_id=request_id,
        needed_position=needed_position,
        absent_skill=absent_skill,
        game_start=game_start,
        as_of=as_of,
    )


def make_candidate(
    player_id,
    primary_position=Position.FORWARD,
    also_plays=(),
    skill=4,
    completed_sub_games=0,
    min_notice_minutes=0,
):
    return SubstituteCandidate(
        player_id=player_id,
        primary_position=primary_position,
        also_plays=frozenset(also_plays),
        skill=skill,
        completed_sub_games=completed_sub_games,
        min_notice_minutes=min_notice_minutes,
    )


def policy_of(*kinds, league_id="league_1", random_seed=None, notice=True):
    """A policy whose enabled rules are exactly ``kinds``, in that order."""
    return LeagueRankingPolicy(
        league_id=league_id,
        rules=tuple(RuleSetting(kind=kind) for kind in kinds),
        notice_window_enabled=notice,
        random_seed=random_seed,
    )


class GoalieSeparationTest(unittest.TestCase):
    """Goalie/skater separation is a HARD exclusion, in BOTH directions."""

    def test_goalie_is_excluded_from_a_skater_slot(self):
        request = make_request(needed_position=Position.FORWARD)
        result = rank_substitutes(
            request,
            [make_candidate("player_g", primary_position=Position.GOALIE)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertEqual(result.ranked, ())
        self.assertIsNone(result.proposed_player_id)
        self.assertEqual(len(result.excluded), 1)
        excluded = result.excluded[0]
        self.assertFalse(excluded.eligible)
        self.assertIsNone(excluded.rank)
        self.assertEqual(excluded.rule_traces, ())
        self.assertEqual(
            excluded.exclusions,
            (Exclusion(gate=GateKind.GOALIE_SEPARATION,
                       detail="goalie may not fill a skater slot"),),
        )

    def test_skater_is_excluded_from_a_goalie_slot(self):
        request = make_request(needed_position=Position.GOALIE)
        result = rank_substitutes(
            request,
            [make_candidate("player_f", primary_position=Position.FORWARD)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertIsNone(result.proposed_player_id)
        self.assertEqual(
            result.excluded[0].exclusions[0].detail,
            "skater may not fill a goalie slot",
        )

    def test_goalie_fills_a_goalie_slot(self):
        request = make_request(needed_position=Position.GOALIE)
        result = rank_substitutes(
            request,
            [make_candidate("player_g", primary_position=Position.GOALIE)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertEqual(result.proposed_player_id, "player_g")
        self.assertEqual(result.excluded, ())

    def test_separation_cannot_be_outranked_by_rule_order(self):
        """No rule ordering makes a goalie eligible for a skater slot: the
        wall is a gate, not a ranking penalty."""
        request = make_request()
        goalie = make_candidate("player_g", primary_position=Position.GOALIE,
                                completed_sub_games=0)
        skater = make_candidate("player_f", primary_position=Position.FORWARD,
                                completed_sub_games=99)
        for order in itertools.permutations(
            [RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
             RuleKind.POSITION_PREFERENCE]
        ):
            result = rank_substitutes(request, [goalie, skater],
                                      policy_of(*order))
            self.assertEqual(result.proposed_player_id, "player_f", msg=str(order))
            self.assertEqual([e.player_id for e in result.excluded], ["player_g"])

    def test_dual_registered_goalie_is_treated_as_a_goalie(self):
        """ASSUMPTION under test: a player registered for goalie in any
        capacity is barred from skater slots (strictest reading)."""
        request = make_request()
        dual = make_candidate("player_d", primary_position=Position.FORWARD,
                              also_plays=[Position.GOALIE])
        result = rank_substitutes(request, [dual], policy_of(RuleKind.FAIRNESS))
        self.assertIsNone(result.proposed_player_id)
        self.assertEqual(result.excluded[0].exclusions[0].gate,
                         GateKind.GOALIE_SEPARATION)


class NoticeWindowTest(unittest.TestCase):
    def test_exactly_enough_notice_is_eligible(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=600))
        result = rank_substitutes(
            request,
            [make_candidate("player_exact", min_notice_minutes=600)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertEqual(result.proposed_player_id, "player_exact")
        self.assertEqual(result.excluded, ())

    def test_zero_notice_at_puck_drop_is_eligible(self):
        request = make_request(as_of=GAME_START)
        result = rank_substitutes(
            request,
            [make_candidate("player_zero", min_notice_minutes=0)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertEqual(request.minutes_remaining, 0)
        self.assertEqual(result.proposed_player_id, "player_zero")
        self.assertEqual(result.excluded, ())

    def test_one_minute_short_is_excluded(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=600))
        result = rank_substitutes(
            request,
            [make_candidate("player_short", min_notice_minutes=601)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertIsNone(result.proposed_player_id)
        self.assertEqual(
            result.excluded[0].exclusions,
            (Exclusion(gate=GateKind.NOTICE_WINDOW,
                       detail="needs 601 min notice, 600 min remain"),),
        )

    def test_partial_minute_is_floored_not_rounded_up(self):
        """59m30s of remaining time does not satisfy a 60 minute requirement."""
        request = make_request(as_of=GAME_START - timedelta(minutes=59, seconds=30))
        result = rank_substitutes(
            request,
            [make_candidate("player_p", min_notice_minutes=60)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertIsNone(result.proposed_player_id)
        self.assertEqual(result.excluded[0].exclusions[0].detail,
                         "needs 60 min notice, 59 min remain")

    def test_game_already_started_excludes_even_zero_notice_candidates(self):
        """A negative notice window never becomes an eligibility shortcut."""
        for minutes_underway in (1, 30, 180):
            with self.subTest(minutes_underway=minutes_underway):
                request = make_request(
                    as_of=GAME_START + timedelta(minutes=minutes_underway)
                )
                result = rank_substitutes(
                    request,
                    [make_candidate("player_zero", min_notice_minutes=0)],
                    policy_of(RuleKind.FAIRNESS),
                )
                self.assertIsNone(result.proposed_player_id)
                self.assertEqual(
                    result.excluded[0].exclusions,
                    (
                        Exclusion(
                            gate=GateKind.NOTICE_WINDOW,
                            detail=(
                                "needs 0 min notice, "
                                f"-{minutes_underway} min remain"
                            ),
                        ),
                    ),
                )

    def test_notice_window_can_be_disabled_by_the_league(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=60))
        candidate = make_candidate("player_late", min_notice_minutes=6000)

        on = rank_substitutes(request, [candidate],
                              policy_of(RuleKind.FAIRNESS, notice=True))
        off = rank_substitutes(request, [candidate],
                               policy_of(RuleKind.FAIRNESS, notice=False))

        self.assertIsNone(on.proposed_player_id)
        self.assertEqual(on.gates_applied,
                         (GateKind.GOALIE_SEPARATION, GateKind.NOTICE_WINDOW))
        self.assertEqual(off.proposed_player_id, "player_late")
        self.assertEqual(off.gates_applied, (GateKind.GOALIE_SEPARATION,))

    def test_both_gate_failures_are_reported_no_short_circuit(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=60))
        candidate = make_candidate("player_both", primary_position=Position.GOALIE,
                                   min_notice_minutes=600)
        result = rank_substitutes(request, [candidate], policy_of(RuleKind.FAIRNESS))
        gates = [exclusion.gate for exclusion in result.excluded[0].exclusions]
        self.assertEqual(gates,
                         [GateKind.GOALIE_SEPARATION, GateKind.NOTICE_WINDOW])


class FairnessTest(unittest.TestCase):
    def test_fewer_completed_substitute_games_ranks_first(self):
        request = make_request()
        candidates = [
            make_candidate("player_a", completed_sub_games=5),
            make_candidate("player_b", completed_sub_games=0),
            make_candidate("player_c", completed_sub_games=2),
        ]
        result = rank_substitutes(request, candidates, policy_of(RuleKind.FAIRNESS))
        self.assertEqual([e.player_id for e in result.ranked],
                         ["player_b", "player_c", "player_a"])
        self.assertEqual([e.rank for e in result.ranked], [1, 2, 3])
        self.assertEqual(result.proposed_player_id, "player_b")
        self.assertEqual(result.deciding_rule, RuleKind.FAIRNESS)


class SkillMatchTest(unittest.TestCase):
    def test_closest_skill_wins(self):
        request = make_request(absent_skill=4)
        candidates = [
            make_candidate("player_far", skill=7),
            make_candidate("player_near", skill=5),
        ]
        result = rank_substitutes(request, candidates,
                                  policy_of(RuleKind.SKILL_MATCH))
        self.assertEqual(result.proposed_player_id, "player_near")

    def test_distance_is_symmetric_so_either_side_ties(self):
        """absent=4; candidates 3 and 5 are equally good."""
        request = make_request(absent_skill=4)
        below = make_candidate("player_below", skill=3)
        above = make_candidate("player_above", skill=5)
        result = rank_substitutes(request, [below, above],
                                  policy_of(RuleKind.SKILL_MATCH))
        values = [e.rule_traces[0].value for e in result.ranked]
        self.assertEqual(values, [1, 1])
        # Nothing but the player_id tiebreak separated them, and the summary
        # says so honestly rather than crediting a rule.
        self.assertIsNone(result.deciding_rule)
        self.assertIn("no rule separated them", result.decision_summary)

    def test_unknown_skill_scores_the_worst_distance(self):
        """Owner ruling: an unrated candidate must never beat a rated one."""
        request = make_request(absent_skill=4)
        known = make_candidate("player_known", skill=7)  # distance 3
        unknown = make_candidate("player_unknown", skill=None)
        result = rank_substitutes(request, [unknown, known],
                                  policy_of(RuleKind.SKILL_MATCH))
        self.assertEqual(result.proposed_player_id, "player_known")
        self.assertEqual(result.ranked[1].rule_traces[0].value, 7)
        self.assertIn("candidate skill unknown",
                      result.ranked[1].rule_traces[0].detail)

    def test_unknown_skill_ranks_after_even_the_farthest_known_rating(self):
        """Owner ruling: unrated candidates follow every rated candidate.

        The maximum real distance on the 1..7 scale is six.  Unknown therefore
        needs a distinct value beyond six; tying it with the farthest known
        rating lets the player-id fallback put the unrated candidate first.
        """
        request = make_request(absent_skill=1)
        farthest_known = make_candidate("player_z_rated", skill=7)
        unknown = make_candidate("player_a_unrated", skill=None)

        result = rank_substitutes(
            request,
            [unknown, farthest_known],
            policy_of(RuleKind.SKILL_MATCH),
        )

        self.assertEqual(
            [evaluation.player_id for evaluation in result.ranked],
            ["player_z_rated", "player_a_unrated"],
        )
        self.assertGreater(
            result.ranked[1].rule_traces[0].value,
            result.ranked[0].rule_traces[0].value,
        )

    def test_unrated_stays_last_at_every_rule_priority_with_all_other_rules_hostile(self):
        checked = 0
        rule_orders = tuple(itertools.permutations(tuple(RuleKind)))
        for absent_skill in (None, *range(1, 8)):
            for rated_skill in range(1, 8):
                request = make_request(
                    absent_skill=absent_skill,
                    needed_position=Position.FORWARD,
                )
                # Every other comparison favours the unrated player: fairness,
                # exact position, seeded draw, and final id. The settled rule is
                # stronger than configurable priority: whenever SKILL_MATCH is
                # enabled, the rated/unrated partition comes first.
                rated = make_candidate(
                    "player_z_rated",
                    primary_position=Position.DEFENSE,
                    skill=rated_skill,
                    completed_sub_games=99,
                )
                unrated = make_candidate(
                    "player_a_unrated",
                    primary_position=Position.FORWARD,
                    skill=None,
                    completed_sub_games=0,
                )
                for order in rule_orders:
                    result = rank_substitutes(
                        request,
                        [unrated, rated],
                        policy_of(*order, random_seed=3),
                    )
                    self.assertEqual(
                        result.proposed_player_id,
                        "player_z_rated",
                        msg=(
                            f"absent={absent_skill} rated={rated_skill} "
                            f"order={[kind.name for kind in order]}"
                        ),
                    )
                    checked += 1
        self.assertEqual(checked, 8 * 7 * len(rule_orders))

    def test_all_rated_or_all_unrated_candidates_keep_configured_rule_order(self):
        request = make_request(absent_skill=4)
        for skills in ((4, 7), (None, None)):
            with self.subTest(skills=skills):
                fair = make_candidate(
                    "player_fair",
                    skill=skills[0],
                    completed_sub_games=0,
                )
                served = make_candidate(
                    "player_served",
                    skill=skills[1],
                    completed_sub_games=5,
                )
                result = rank_substitutes(
                    request,
                    [served, fair],
                    policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH),
                )
                self.assertEqual(result.proposed_player_id, "player_fair")

    def test_unrated_candidate_stays_last_when_absent_skill_is_unknown(self):
        request = make_request(absent_skill=None)
        rated = make_candidate(
            "player_z_rated", skill=4, completed_sub_games=99
        )
        unrated = make_candidate(
            "player_a_unrated", skill=None, completed_sub_games=0
        )
        result = rank_substitutes(
            request,
            [unrated, rated],
            policy_of(RuleKind.SKILL_MATCH, RuleKind.FAIRNESS),
        )
        self.assertEqual(result.proposed_player_id, "player_z_rated")
        self.assertEqual(result.ranked[0].rule_traces[0].value, 0)
        self.assertEqual(result.ranked[1].rule_traces[0].value, 7)


class PositionPreferenceTest(unittest.TestCase):
    def test_exact_beats_both_beats_alternate(self):
        request = make_request(needed_position=Position.FORWARD)
        exact = make_candidate("player_exact", primary_position=Position.FORWARD)
        both = make_candidate("player_both", primary_position=Position.DEFENSE,
                              also_plays=[Position.FORWARD])
        alternate = make_candidate("player_alt", primary_position=Position.DEFENSE)
        result = rank_substitutes(
            request, [alternate, both, exact],
            policy_of(RuleKind.POSITION_PREFERENCE),
        )
        self.assertEqual([e.player_id for e in result.ranked],
                         ["player_exact", "player_both", "player_alt"])
        self.assertEqual([e.rule_traces[0].value for e in result.ranked], [0, 1, 2])

    def test_generic_skater_lands_in_the_alternate_tier(self):
        """ASSUMPTION under test: an unspecified position is not evidence of
        versatility, so it ranks below a player who plays both."""
        request = make_request(needed_position=Position.FORWARD)
        generic = make_candidate("player_generic", primary_position=Position.SKATER)
        both = make_candidate("player_both", primary_position=Position.DEFENSE,
                              also_plays=[Position.FORWARD])
        result = rank_substitutes(request, [generic, both],
                                  policy_of(RuleKind.POSITION_PREFERENCE))
        self.assertEqual([e.player_id for e in result.ranked],
                         ["player_both", "player_generic"])
        self.assertEqual(result.ranked[1].rule_traces[0].value, 2)

    def test_generic_skater_vacancy_does_not_invent_a_position_preference(self):
        request = make_request(needed_position=Position.SKATER)
        pool = [
            make_candidate("player_d_generic", primary_position=Position.SKATER),
            make_candidate(
                "player_c_both",
                primary_position=Position.DEFENSE,
                also_plays=[Position.FORWARD],
            ),
            make_candidate("player_b_forward", primary_position=Position.FORWARD),
            make_candidate("player_a_defense", primary_position=Position.DEFENSE),
        ]
        result = rank_substitutes(
            request,
            pool,
            policy_of(RuleKind.POSITION_PREFERENCE),
        )
        self.assertEqual(
            [evaluation.player_id for evaluation in result.ranked],
            [
                "player_a_defense",
                "player_b_forward",
                "player_c_both",
                "player_d_generic",
            ],
        )
        self.assertEqual(
            [evaluation.rule_traces[0].value for evaluation in result.ranked],
            [0, 0, 0, 0],
        )
        self.assertTrue(
            all(
                evaluation.rule_traces[0].detail
                == "generic skater slot; no specific position preference"
                for evaluation in result.ranked
            )
        )

    def test_every_vacancy_position_has_an_explicit_ranking_oracle(self):
        """Derive the coverage axis from Position so DEFENSE cannot disappear."""
        for needed_position in tuple(Position):
            with self.subTest(needed_position=needed_position):
                request = make_request(needed_position=needed_position)
                if needed_position is Position.GOALIE:
                    pool = [
                        make_candidate(
                            "player_goalie", primary_position=Position.GOALIE
                        )
                    ]
                    expected_ids = ["player_goalie"]
                    expected_values = [0]
                elif needed_position is Position.SKATER:
                    pool = [
                        make_candidate(
                            "player_forward", primary_position=Position.FORWARD
                        ),
                        make_candidate(
                            "player_defense", primary_position=Position.DEFENSE
                        ),
                    ]
                    expected_ids = ["player_defense", "player_forward"]
                    expected_values = [0, 0]
                else:
                    alternate_position = (
                        Position.DEFENSE
                        if needed_position is Position.FORWARD
                        else Position.FORWARD
                    )
                    pool = [
                        make_candidate(
                            "player_alternate",
                            primary_position=alternate_position,
                        ),
                        make_candidate(
                            "player_both",
                            primary_position=alternate_position,
                            also_plays=[needed_position],
                        ),
                        make_candidate(
                            "player_exact",
                            primary_position=needed_position,
                        ),
                    ]
                    expected_ids = [
                        "player_exact",
                        "player_both",
                        "player_alternate",
                    ]
                    expected_values = [0, 1, 2]

                result = rank_substitutes(
                    request,
                    pool,
                    policy_of(RuleKind.POSITION_PREFERENCE),
                )
                self.assertEqual(
                    [evaluation.player_id for evaluation in result.ranked],
                    expected_ids,
                )
                self.assertEqual(
                    [evaluation.rule_traces[0].value for evaluation in result.ranked],
                    expected_values,
                )
                self.assertTrue(
                    all(evaluation.eligible for evaluation in result.ranked)
                )


class LeagueRuleOrderTest(unittest.TestCase):
    """The configurability must be load-bearing, not decorative."""

    def setUp(self):
        self.request = make_request(absent_skill=4)
        # player_fair: never subbed, but skill is 3 away.
        # player_skill: perfect skill match, but has subbed twice.
        self.pool = [
            make_candidate("player_fair", skill=7, completed_sub_games=0),
            make_candidate("player_skill", skill=4, completed_sub_games=2),
        ]

    def test_fairness_first_picks_the_fair_candidate(self):
        result = rank_substitutes(
            self.request, self.pool,
            policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH),
        )
        self.assertEqual(result.proposed_player_id, "player_fair")
        self.assertEqual(result.deciding_rule, RuleKind.FAIRNESS)
        self.assertEqual(result.rules_applied,
                         (RuleKind.FAIRNESS, RuleKind.SKILL_MATCH))

    def test_skill_first_picks_the_other_candidate_over_the_same_pool(self):
        result = rank_substitutes(
            self.request, self.pool,
            policy_of(RuleKind.SKILL_MATCH, RuleKind.FAIRNESS),
        )
        self.assertEqual(result.proposed_player_id, "player_skill")
        self.assertEqual(result.deciding_rule, RuleKind.SKILL_MATCH)
        self.assertEqual(result.rules_applied,
                         (RuleKind.SKILL_MATCH, RuleKind.FAIRNESS))

    def test_two_leagues_disagree_about_the_same_pool(self):
        east = rank_substitutes(
            self.request, self.pool,
            policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
                      league_id="league_east"),
        )
        west = rank_substitutes(
            self.request, self.pool,
            policy_of(RuleKind.SKILL_MATCH, RuleKind.FAIRNESS,
                      league_id="league_west"),
        )
        self.assertNotEqual(east.proposed_player_id, west.proposed_player_id)
        self.assertEqual(east.league_id, "league_east")
        self.assertEqual(west.league_id, "league_west")


class DisabledRuleTest(unittest.TestCase):
    def test_disabled_rule_is_absent_from_the_output_and_the_sort_key(self):
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("player_fair", skill=7, completed_sub_games=0),
            make_candidate("player_skill", skill=4, completed_sub_games=2),
        ]
        policy = LeagueRankingPolicy(
            league_id="league_1",
            rules=(
                RuleSetting(kind=RuleKind.FAIRNESS, enabled=False),
                RuleSetting(kind=RuleKind.SKILL_MATCH),
            ),
        )
        result = rank_substitutes(request, pool, policy)

        self.assertEqual(result.rules_applied, (RuleKind.SKILL_MATCH,))
        for evaluation in result.ranked:
            kinds = [trace.rule for trace in evaluation.rule_traces]
            self.assertEqual(kinds, [RuleKind.SKILL_MATCH])
            # Omission, not zero-weighting: one settled rated/unrated partition,
            # one SKILL_MATCH value, and player_id. The disabled FAIRNESS value
            # is absent.
            self.assertEqual(len(evaluation.sort_key), 3)
        # FAIRNESS is listed first but disabled, so it does not decide anything.
        self.assertEqual(result.proposed_player_id, "player_skill")
        self.assertEqual(result.deciding_rule, RuleKind.SKILL_MATCH)

    def test_disabled_rule_matches_a_policy_that_never_listed_it(self):
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("player_fair", skill=7, completed_sub_games=0),
            make_candidate("player_skill", skill=4, completed_sub_games=2),
        ]
        disabled = LeagueRankingPolicy(
            league_id="league_1",
            rules=(RuleSetting(kind=RuleKind.FAIRNESS, enabled=False),
                   RuleSetting(kind=RuleKind.SKILL_MATCH)),
        )
        omitted = policy_of(RuleKind.SKILL_MATCH)
        self.assertEqual(
            [e.sort_key for e in rank_substitutes(request, pool, disabled).ranked],
            [e.sort_key for e in rank_substitutes(request, pool, omitted).ranked],
        )

    def test_priority_index_reflects_configured_position_not_enabled_position(self):
        request = make_request()
        policy = LeagueRankingPolicy(
            league_id="league_1",
            rules=(
                RuleSetting(kind=RuleKind.FAIRNESS, enabled=False),
                RuleSetting(kind=RuleKind.SKILL_MATCH),
            ),
        )
        result = rank_substitutes(request, [make_candidate("player_a")], policy)
        self.assertEqual(result.ranked[0].rule_traces[0].priority, 1)


class SeededRandomTest(unittest.TestCase):
    POOL_IDS = [f"player_{index}" for index in range(8)]

    def _pool(self):
        return [make_candidate(player_id) for player_id in self.POOL_IDS]

    def test_unseeded_random_is_impossible_by_construction(self):
        with self.assertRaises(PolicyError) as caught:
            LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.RANDOM),),
            )
        self.assertIn("random_seed", str(caught.exception))

    def test_disabled_random_needs_no_seed(self):
        policy = LeagueRankingPolicy(
            league_id="league_1",
            rules=(RuleSetting(kind=RuleKind.RANDOM, enabled=False),
                   RuleSetting(kind=RuleKind.FAIRNESS)),
        )
        self.assertEqual(policy.enabled_rules, (RuleKind.FAIRNESS,))

    def test_same_seed_reproduces_the_same_order_across_runs(self):
        request = make_request()
        policy = policy_of(RuleKind.RANDOM, random_seed=20260901)
        first = rank_substitutes(request, self._pool(), policy)
        second = rank_substitutes(request, self._pool(), policy)
        self.assertEqual([e.player_id for e in first.ranked],
                         [e.player_id for e in second.ranked])
        self.assertEqual([e.sort_key for e in first.ranked],
                         [e.sort_key for e in second.ranked])

    def test_random_draw_is_scoped_to_a_stable_request_id(self):
        policy = policy_of(RuleKind.RANDOM, random_seed=7)
        first_request = rank_substitutes(
            make_request(request_id="request_a"), self._pool(), policy
        )
        same_request_retry = rank_substitutes(
            make_request(request_id="request_a"), self._pool(), policy
        )
        different_vacancy = rank_substitutes(
            make_request(request_id="request_b"), self._pool(), policy
        )
        self.assertEqual(
            [evaluation.player_id for evaluation in first_request.ranked],
            [evaluation.player_id for evaluation in same_request_retry.ranked],
        )
        self.assertNotEqual(
            [evaluation.player_id for evaluation in first_request.ranked],
            [evaluation.player_id for evaluation in different_vacancy.ranked],
        )

    def test_a_different_seed_changes_the_draw(self):
        request = make_request()
        one = rank_substitutes(request, self._pool(),
                               policy_of(RuleKind.RANDOM, random_seed=1))
        two = rank_substitutes(request, self._pool(),
                               policy_of(RuleKind.RANDOM, random_seed=2))
        self.assertNotEqual([e.player_id for e in one.ranked],
                            [e.player_id for e in two.ranked])

    def test_random_as_a_tiebreaker_only_breaks_actual_ties(self):
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("player_low", completed_sub_games=0, skill=4),
            make_candidate("player_tie_a", completed_sub_games=3, skill=4),
            make_candidate("player_tie_b", completed_sub_games=3, skill=4),
        ]
        result = rank_substitutes(
            request, pool,
            policy_of(RuleKind.FAIRNESS, RuleKind.RANDOM, random_seed=7),
        )
        # FAIRNESS still decides the winner; RANDOM only orders the tied pair.
        self.assertEqual(result.proposed_player_id, "player_low")
        self.assertEqual(result.deciding_rule, RuleKind.FAIRNESS)
        self.assertEqual(sorted(e.player_id for e in result.ranked[1:]),
                         ["player_tie_a", "player_tie_b"])

    def test_random_draw_does_not_depend_on_candidate_list_order(self):
        request = make_request()
        policy = policy_of(RuleKind.RANDOM, random_seed=99)
        baseline = rank_substitutes(request, self._pool(), policy)
        pool = self._pool()
        for permutation in itertools.permutations(pool[:5]):
            shuffled = list(permutation) + pool[5:]
            result = rank_substitutes(request, shuffled, policy)
            self.assertEqual([e.player_id for e in result.ranked],
                             [e.player_id for e in baseline.ranked])


class DeterminismTest(unittest.TestCase):
    def test_shuffling_the_input_never_changes_the_result(self):
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("player_a", skill=4, completed_sub_games=1),
            make_candidate("player_b", skill=4, completed_sub_games=1),
            make_candidate("player_c", skill=5, completed_sub_games=1,
                           primary_position=Position.DEFENSE),
            make_candidate("player_g", primary_position=Position.GOALIE),
            make_candidate("player_late", skill=4, min_notice_minutes=99999),
        ]
        policy = policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
                           RuleKind.POSITION_PREFERENCE, RuleKind.RANDOM,
                           random_seed=4242)
        baseline = rank_substitutes(request, pool, policy)
        for permutation in itertools.permutations(pool):
            result = rank_substitutes(request, list(permutation), policy)
            self.assertEqual([e.player_id for e in result.ranked],
                             [e.player_id for e in baseline.ranked])
            self.assertEqual([e.sort_key for e in result.ranked],
                             [e.sort_key for e in baseline.ranked])
            # Excluded candidates are reported in a stable, order-independent
            # order too.
            self.assertEqual([e.player_id for e in result.excluded],
                             ["player_g", "player_late"])
            self.assertEqual(result.decision_summary, baseline.decision_summary)

    def test_identical_candidates_are_separated_by_the_player_id_tiebreak(self):
        request = make_request()
        pool = [make_candidate("player_z"), make_candidate("player_a")]
        result = rank_substitutes(request, pool, policy_of(RuleKind.FAIRNESS))
        self.assertEqual([e.player_id for e in result.ranked],
                         ["player_a", "player_z"])
        self.assertIsNone(result.deciding_rule)


class ExplanationTest(unittest.TestCase):
    def test_winner_summary_names_the_deciding_rule_and_the_values(self):
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("player_busy", completed_sub_games=2, skill=4),
            make_candidate("player_free", completed_sub_games=0, skill=4),
        ]
        result = rank_substitutes(request, pool,
                                  policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH))
        self.assertEqual(result.deciding_rule, RuleKind.FAIRNESS)
        self.assertEqual(
            result.decision_summary,
            "player_free proposed: FAIRNESS (0 completed substitute games vs "
            "player_busy: 2 completed substitute games)",
        )

    def test_every_eligible_candidate_carries_a_trace_per_enabled_rule(self):
        request = make_request(absent_skill=4, needed_position=Position.FORWARD)
        pool = [
            make_candidate("player_a", skill=5, completed_sub_games=1),
            make_candidate("player_b", skill=4, completed_sub_games=0,
                           primary_position=Position.DEFENSE),
        ]
        policy = policy_of(RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
                           RuleKind.POSITION_PREFERENCE)
        result = rank_substitutes(request, pool, policy)
        for evaluation in result.ranked:
            self.assertEqual(
                [trace.rule for trace in evaluation.rule_traces],
                [RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
                 RuleKind.POSITION_PREFERENCE],
            )
            for trace in evaluation.rule_traces:
                self.assertTrue(trace.detail)
        traces = {t.rule: t.detail for t in result.ranked[0].rule_traces}
        self.assertEqual(traces[RuleKind.FAIRNESS], "0 completed substitute games")
        self.assertEqual(traces[RuleKind.SKILL_MATCH],
                         "skill 4 vs 4 (distance 0)")
        self.assertEqual(traces[RuleKind.POSITION_PREFERENCE],
                         "alternate position (defense for a forward slot)")

    def test_every_excluded_candidate_names_its_exclusion_reason(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=540))
        pool = [
            make_candidate("player_g", primary_position=Position.GOALIE),
            make_candidate("player_late", min_notice_minutes=720),
            make_candidate("player_ok"),
        ]
        result = rank_substitutes(request, pool,
                                  policy_of(RuleKind.FAIRNESS))
        details = {e.player_id: [x.detail for x in e.exclusions]
                   for e in result.excluded}
        self.assertEqual(details, {
            "player_g": ["goalie may not fill a skater slot"],
            "player_late": ["needs 720 min notice, 540 min remain"],
        })
        self.assertEqual(result.proposed_player_id, "player_ok")

    def test_sole_eligible_candidate_is_explained_honestly(self):
        request = make_request()
        result = rank_substitutes(
            request,
            [make_candidate("player_only"),
             make_candidate("player_g", primary_position=Position.GOALIE)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertIsNone(result.deciding_rule)
        self.assertEqual(result.decision_summary,
                         "player_only proposed: sole eligible candidate")

    def test_empty_eligible_pool_is_a_normal_result_not_an_exception(self):
        request = make_request()
        result = rank_substitutes(
            request,
            [make_candidate("player_g", primary_position=Position.GOALIE)],
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertIsNone(result.proposed_player_id)
        self.assertIsNone(result.selected_player_id)
        self.assertEqual(result.decision_summary, "no eligible candidate")
        self.assertEqual(len(result.excluded), 1)

    def test_empty_candidate_pool_is_a_normal_result(self):
        result = rank_substitutes(make_request(), [], policy_of(RuleKind.FAIRNESS))
        self.assertEqual(result.ranked, ())
        self.assertEqual(result.excluded, ())
        self.assertIsNone(result.proposed_player_id)


class OverrideTest(unittest.TestCase):
    def test_override_replaces_the_selection_but_not_the_proposal(self):
        request = make_request()
        pool = [
            make_candidate("player_free", completed_sub_games=0),
            make_candidate("player_busy", completed_sub_games=5),
        ]
        override = SelectionOverride(player_id="player_busy", actor_id="user_coach",
                                     reason="carpools with the captain")
        result = rank_substitutes(request, pool, policy_of(RuleKind.FAIRNESS),
                                  override=override)
        self.assertEqual(result.proposed_player_id, "player_free")
        self.assertEqual(result.selected_player_id, "player_busy")
        self.assertTrue(result.override_applied)
        self.assertIsNone(result.override_refusal)
        self.assertEqual(result.override_conflicts, ())
        self.assertIn("overridden by user_coach to player_busy",
                      result.decision_summary)
        self.assertIn("carpools with the captain", result.decision_summary)
        self.assertIn("engine proposal (not selected): player_free proposed",
                      result.decision_summary)

    def test_override_confirming_the_proposal_does_not_call_it_unselected(self):
        request = make_request()
        pool = [
            make_candidate("player_a", completed_sub_games=0),
            make_candidate("player_b", completed_sub_games=5),
        ]
        override = SelectionOverride(
            player_id="player_a",
            actor_id="coach_1",
            reason="same pick",
        )

        result = rank_substitutes(
            request,
            pool,
            policy_of(RuleKind.FAIRNESS),
            override=override,
        )

        self.assertEqual(result.proposed_player_id, "player_a")
        self.assertEqual(result.selected_player_id, "player_a")
        self.assertTrue(result.override_applied)
        self.assertIsNone(result.deciding_rule)
        self.assertTrue(result.decision_summary.startswith("player_a selected:"))
        self.assertIn("coach_1", result.decision_summary)
        self.assertIn("same pick", result.decision_summary)
        self.assertIn("confirmed the engine proposal", result.decision_summary)
        self.assertIn("engine proposal (selected):", result.decision_summary)
        self.assertNotIn("not selected", result.decision_summary)

    def test_override_onto_a_gated_player_is_refused(self):
        request = make_request()
        pool = [
            make_candidate("player_free"),
            make_candidate("player_g", primary_position=Position.GOALIE),
        ]
        override = SelectionOverride(player_id="player_g", actor_id="user_admin",
                                     reason="dressed as a skater tonight")
        result = rank_substitutes(request, pool, policy_of(RuleKind.FAIRNESS),
                                  override=override)
        self.assertEqual(result.selected_player_id, "player_free")
        self.assertFalse(result.override_applied)
        self.assertEqual(result.override_refusal.kind,
                         OverrideRefusalKind.INELIGIBLE)
        self.assertEqual(result.override_conflicts[0].gate,
                         GateKind.GOALIE_SEPARATION)
        self.assertIn("override by user_admin to player_g refused",
                      result.decision_summary)
        self.assertIn("goalie may not fill a skater slot",
                      result.decision_summary)

    def test_override_cannot_put_a_skater_in_a_goalie_slot(self):
        request = make_request(needed_position=Position.GOALIE)
        pool = [
            make_candidate("player_goalie", primary_position=Position.GOALIE),
            make_candidate("player_skater", primary_position=Position.FORWARD),
        ]
        override = SelectionOverride(
            player_id="player_skater",
            actor_id="user_admin",
            reason="emergency",
        )
        result = rank_substitutes(
            request,
            pool,
            policy_of(RuleKind.FAIRNESS),
            override=override,
        )
        self.assertFalse(result.override_applied)
        self.assertEqual(result.selected_player_id, "player_goalie")
        self.assertEqual(result.override_refusal.kind,
                         OverrideRefusalKind.INELIGIBLE)
        self.assertEqual(result.override_conflicts[0].gate,
                         GateKind.GOALIE_SEPARATION)

    def test_override_naming_an_unknown_player_is_reported_not_silently_clean(self):
        """ASSUMPTION under test: the engine cannot evaluate gates for a player
        it has no data on, and says so instead of implying a clean check."""
        request = make_request()
        override = SelectionOverride(player_id="player_ghost", actor_id="user_admin",
                                     reason="showed up at the rink")
        result = rank_substitutes(request, [make_candidate("player_free")],
                                  policy_of(RuleKind.FAIRNESS), override=override)
        self.assertEqual(result.selected_player_id, "player_free")
        self.assertFalse(result.override_applied)
        self.assertEqual(result.override_refusal.kind,
                         OverrideRefusalKind.UNKNOWN_CANDIDATE)
        self.assertEqual(result.override_conflicts, ())
        self.assertIn("not in the candidate pool; eligibility was not evaluated",
                      result.decision_summary)

    def test_ineligible_override_does_not_fill_an_empty_eligible_pool(self):
        request = make_request()
        override = SelectionOverride(player_id="player_g", actor_id="user_admin",
                                     reason="only body available")
        result = rank_substitutes(
            request,
            [make_candidate("player_g", primary_position=Position.GOALIE)],
            policy_of(RuleKind.FAIRNESS), override=override)
        self.assertIsNone(result.proposed_player_id)
        self.assertIsNone(result.selected_player_id)
        self.assertFalse(result.override_applied)
        self.assertEqual(result.override_refusal.kind,
                         OverrideRefusalKind.INELIGIBLE)
        self.assertEqual(len(result.override_conflicts), 1)

    def test_notice_failed_override_is_refused_and_keeps_attribution(self):
        request = make_request(as_of=GAME_START - timedelta(minutes=60))
        pool = [
            make_candidate("player_free", completed_sub_games=0),
            make_candidate("player_busy", completed_sub_games=5),
            make_candidate("player_late", min_notice_minutes=61),
        ]
        baseline = rank_substitutes(
            request, pool, policy_of(RuleKind.FAIRNESS)
        )
        override = SelectionOverride(
            player_id="player_late",
            actor_id="user_coach",
            reason="asked for a late exception",
        )

        result = rank_substitutes(
            request,
            pool,
            policy_of(RuleKind.FAIRNESS),
            override=override,
        )

        self.assertFalse(result.override_applied)
        self.assertEqual(result.override_refusal.kind,
                         OverrideRefusalKind.INELIGIBLE)
        self.assertEqual(result.selected_player_id, baseline.selected_player_id)
        self.assertEqual(result.deciding_rule, baseline.deciding_rule)
        self.assertEqual(result.override_conflicts[0].gate,
                         GateKind.NOTICE_WINDOW)


class PolicyValidationTest(unittest.TestCase):
    def test_zero_rules_is_refused(self):
        with self.assertRaises(PolicyError):
            LeagueRankingPolicy(league_id="league_1", rules=())

    def test_all_rules_disabled_is_refused(self):
        with self.assertRaises(PolicyError):
            LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.FAIRNESS, enabled=False),),
            )

    def test_duplicate_rule_kind_is_refused(self):
        with self.assertRaises(PolicyError):
            LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.FAIRNESS),
                       RuleSetting(kind=RuleKind.FAIRNESS, enabled=False)),
            )

    def test_malformed_rule_configuration_is_refused_as_policy_error(self):
        malformed = (
            lambda: RuleSetting(kind="bogus"),
            lambda: RuleSetting(kind=RuleKind.FAIRNESS, enabled="yes"),
            lambda: LeagueRankingPolicy(
                league_id="",
                rules=(RuleSetting(kind=RuleKind.FAIRNESS),),
            ),
            lambda: LeagueRankingPolicy(
                league_id=123,
                rules=(RuleSetting(kind=RuleKind.FAIRNESS),),
            ),
            lambda: LeagueRankingPolicy(
                league_id="league_1",
                rules=[RuleSetting(kind=RuleKind.FAIRNESS)],
            ),
            lambda: LeagueRankingPolicy(
                league_id="league_1",
                rules=(object(),),
            ),
            lambda: LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.FAIRNESS),),
                notice_window_enabled="yes",
            ),
            lambda: LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.RANDOM),),
                random_seed=True,
            ),
            lambda: LeagueRankingPolicy(
                league_id="league_1",
                rules=(RuleSetting(kind=RuleKind.RANDOM),),
                random_seed="seed",
            ),
        )
        for build in malformed:
            with self.subTest(build=build):
                with self.assertRaises(PolicyError):
                    build()

    def test_duplicate_player_id_in_the_pool_is_refused(self):
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(),
                [make_candidate("player_a"), make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            )

    def test_one_shot_candidate_iterable_is_snapshotted_once(self):
        pool = (candidate for candidate in [make_candidate("player_a")])
        result = rank_substitutes(
            make_request(),
            pool,
            policy_of(RuleKind.FAIRNESS),
        )
        self.assertEqual(result.proposed_player_id, "player_a")
        self.assertEqual([item.player_id for item in result.ranked], ["player_a"])

    def test_duplicate_ids_in_one_shot_iterable_are_still_refused(self):
        pool = (
            candidate
            for candidate in (
                make_candidate("player_a"),
                make_candidate("player_a"),
            )
        )
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(),
                pool,
                policy_of(RuleKind.FAIRNESS),
            )

    def test_malformed_request_and_candidate_shapes_are_refused(self):
        invalid_calls = (
            lambda: rank_substitutes(
                make_request(request_id=""),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(request_id=123),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(needed_position="forward"),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(),
                [make_candidate("")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(),
                [make_candidate("player_a", primary_position="forward")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(),
                [SubstituteCandidate(
                    player_id="player_a",
                    primary_position=Position.FORWARD,
                    also_plays=[Position.DEFENSE],
                )],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(),
                [object()],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                object(),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            ),
            lambda: rank_substitutes(
                make_request(),
                [make_candidate("player_a")],
                object(),
            ),
            lambda: rank_substitutes(
                make_request(),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
                override=object(),
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(PolicyError):
                    call()

    def test_override_requires_non_empty_audit_fields(self):
        invalid_overrides = (
            SelectionOverride(player_id="", actor_id="user_admin", reason="why"),
            SelectionOverride(player_id="player_a", actor_id="", reason="why"),
            SelectionOverride(player_id="player_a", actor_id="user_admin", reason=" "),
            SelectionOverride(player_id=123, actor_id="user_admin", reason="why"),
            SelectionOverride(player_id="player_a", actor_id=123, reason="why"),
            SelectionOverride(player_id="player_a", actor_id="user_admin", reason=123),
        )
        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises(PolicyError):
                    rank_substitutes(
                        make_request(),
                        [make_candidate("player_a")],
                        policy_of(RuleKind.FAIRNESS),
                        override=override,
                    )

    def test_candidate_skill_outside_the_scale_is_refused(self):
        for skill in OUT_OF_RANGE_SKILLS:
            with self.subTest(skill=skill):
                with self.assertRaises(PolicyError):
                    rank_substitutes(make_request(),
                                     [make_candidate("player_a", skill=skill)],
                                     policy_of(RuleKind.FAIRNESS))

    def test_absent_skill_outside_the_scale_is_refused(self):
        for absent_skill in OUT_OF_RANGE_SKILLS:
            with self.subTest(absent_skill=absent_skill):
                with self.assertRaises(PolicyError):
                    rank_substitutes(
                        make_request(absent_skill=absent_skill),
                        [make_candidate("p")],
                        policy_of(RuleKind.FAIRNESS),
                    )

    def test_skill_fields_require_real_integers(self):
        invalid_skill = 4.5
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(absent_skill=invalid_skill),
                [make_candidate("player_a")],
                policy_of(RuleKind.SKILL_MATCH),
            )
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(),
                [make_candidate("player_a", skill=invalid_skill)],
                policy_of(RuleKind.SKILL_MATCH),
            )

    def test_candidate_player_id_type_errors_are_normalized_to_policy_error(self):
        with self.assertRaises(PolicyError) as caught:
            rank_substitutes(
                make_request(),
                [make_candidate(123), make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            )
        self.assertIn("player_id", str(caught.exception))

    def test_boolean_skill_values_are_refused(self):
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(absent_skill=True),
                [make_candidate("player_a")],
                policy_of(RuleKind.FAIRNESS),
            )
        with self.assertRaises(PolicyError):
            rank_substitutes(
                make_request(),
                [make_candidate("player_a", skill=True)],
                policy_of(RuleKind.FAIRNESS),
            )

    def test_negative_min_notice_is_refused(self):
        with self.assertRaises(PolicyError):
            rank_substitutes(make_request(),
                             [make_candidate("player_a", min_notice_minutes=-1)],
                             policy_of(RuleKind.FAIRNESS))

    def test_negative_completed_substitute_games_is_refused(self):
        with self.assertRaises(PolicyError) as caught:
            rank_substitutes(
                make_request(),
                [make_candidate("player_a", completed_sub_games=-1)],
                policy_of(RuleKind.FAIRNESS),
            )
        self.assertIn("completed_sub_games", str(caught.exception))

    def test_counts_require_real_non_negative_integers(self):
        invalid_values = (True, 1.5, "1")
        for field_name in ("completed_sub_games", "min_notice_minutes"):
            for value in invalid_values:
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaises(PolicyError) as caught:
                        rank_substitutes(
                            make_request(),
                            [make_candidate("player_a", **{field_name: value})],
                            policy_of(RuleKind.FAIRNESS),
                        )
                    self.assertIn(field_name, str(caught.exception))

    def test_request_times_must_be_timezone_aware_utc(self):
        non_utc = timezone(timedelta(hours=5, minutes=30))
        invalid_cases = (
            ("game_start", "2026-09-01T19:00:00Z", AS_OF),
            ("as_of", GAME_START, 0),
            ("game_start", GAME_START.replace(tzinfo=None), AS_OF),
            ("as_of", GAME_START, AS_OF.replace(tzinfo=None)),
            ("game_start", GAME_START.astimezone(non_utc), AS_OF),
            ("as_of", GAME_START, AS_OF.astimezone(non_utc)),
        )
        for field_name, game_start, as_of in invalid_cases:
            with self.subTest(field_name=field_name):
                with self.assertRaises(PolicyError) as caught:
                    rank_substitutes(
                        make_request(game_start=game_start, as_of=as_of),
                        [make_candidate("player_a")],
                        policy_of(RuleKind.FAIRNESS),
                    )
                self.assertIn(field_name, str(caught.exception))
                self.assertIn("timezone-aware UTC", str(caught.exception))


class PurityTest(unittest.TestCase):
    def test_module_performs_no_io_and_reads_no_clock(self):
        import inspect

        from hockey_scheduler.services import substitute_ranking

        source = inspect.getsource(substitute_ranking)
        for forbidden in ("datetime.now(", "datetime.utcnow(", "time.time(",
                          "open(", "requests", "random.random",
                          "random.shuffle", "random.choice"):
            self.assertNotIn(forbidden, source, msg=forbidden)

    def test_ranking_does_not_mutate_its_inputs(self):
        request = make_request()
        pool = [make_candidate("player_b", completed_sub_games=2),
                make_candidate("player_a", completed_sub_games=1)]
        before = list(pool)
        rank_substitutes(request, pool, policy_of(RuleKind.FAIRNESS))
        self.assertEqual(pool, before)
        self.assertEqual([c.player_id for c in pool], ["player_b", "player_a"])


class OnePositionAxisTest(unittest.TestCase):
    """A vacancy has ONE position fact. ``slot_type`` is derived from
    ``needed_position``, so the two can never disagree."""

    def test_slot_type_is_derived_from_the_needed_position(self):
        expected = {
            Position.GOALIE: SlotType.GOALIE,
            Position.FORWARD: SlotType.SKATER,
            Position.DEFENSE: SlotType.SKATER,
            Position.SKATER: SlotType.SKATER,
        }
        for position, slot_type in expected.items():
            self.assertIs(make_request(needed_position=position).slot_type,
                          slot_type, msg=position.value)

    def test_a_disagreeing_slot_type_cannot_be_constructed(self):
        """The inconsistent pair the earlier shape allowed — a GOALIE vacancy
        declared a skater slot, and its mirror — is now unrepresentable:
        ``slot_type`` is not a constructor argument at all."""
        for position in (Position.GOALIE, Position.FORWARD):
            for slot_type in (SlotType.GOALIE, SlotType.SKATER):
                with self.assertRaises(TypeError):
                    SubstituteRequest(
                        request_id="sub_req_1",
                        needed_position=position,
                        slot_type=slot_type,
                        absent_skill=4,
                        game_start=GAME_START,
                        as_of=AS_OF,
                    )

    def test_slot_type_cannot_be_reassigned_after_construction(self):
        """The other route to a disagreeing pair — build it consistent, then
        mutate — is closed too: the request is frozen and slot_type is derived."""
        request = make_request(needed_position=Position.GOALIE)
        with self.assertRaises(AttributeError):
            request.slot_type = SlotType.SKATER
        self.assertIs(request.slot_type, SlotType.GOALIE)

    def test_goalie_separation_holds_for_every_position_combination(self):
        """Exhaustive both-directions sweep over the whole position space.

        For every vacancy position and every registered-position set a
        candidate can have, eligibility must equal "candidate is a goalie ==
        vacancy is a goalie" — nothing else.
        """
        all_positions = list(Position)
        checked = 0
        for needed in all_positions:
            request = make_request(needed_position=needed)
            for primary in all_positions:
                others = [p for p in all_positions if p is not primary]
                for size in range(len(others) + 1):
                    for extra in itertools.combinations(others, size):
                        candidate = make_candidate(
                            "player_x", primary_position=primary,
                            also_plays=extra,
                        )
                        for policy in (
                            policy_of(RuleKind.FAIRNESS),
                            policy_of(RuleKind.POSITION_PREFERENCE,
                                      RuleKind.FAIRNESS),
                        ):
                            result = rank_substitutes(request, [candidate],
                                                      policy)
                            candidate_is_goalie = Position.GOALIE in (
                                {primary} | set(extra)
                            )
                            should_pass = (
                                candidate_is_goalie
                                is (needed is Position.GOALIE)
                            )
                            msg = f"needed={needed} primary={primary} also={extra}"
                            self.assertEqual(
                                result.proposed_player_id == "player_x",
                                should_pass, msg=msg)
                            if not should_pass:
                                self.assertEqual(
                                    result.excluded[0].exclusions[0].gate,
                                    GateKind.GOALIE_SEPARATION, msg=msg)
                            checked += 1
        self.assertEqual(checked, 4 * 4 * 8 * 2)


def _winner_without(request, pool, kinds, drop_index, seed):
    """Independent oracle: who is proposed when rule ``drop_index`` is not in
    the policy at all. Re-ranks from scratch rather than reusing the engine's
    attribution, so it can falsify it."""
    remaining = tuple(kinds[:drop_index]) + tuple(kinds[drop_index + 1:])
    if remaining:
        return rank_substitutes(
            request, pool, policy_of(*remaining, random_seed=seed)
        ).proposed_player_id
    # Dropping the only rule leaves the player_id tiebreak. Gates are unchanged
    # by rule configuration, so the eligible set is the same one.
    full = rank_substitutes(request, pool, policy_of(*kinds, random_seed=seed))
    eligible = [e.player_id for e in full.ranked]
    return min(eligible) if eligible else None


class DecidingRuleDefinitionTest(unittest.TestCase):
    """``proposal_deciding_rules`` == exactly the rules whose DELETION changes
    the proposal. Asserted against a re-ranking oracle, not against a string."""

    def test_underclaim_a_rule_that_decided_the_outcome_is_named(self):
        """Finding (1): the rule that knocked the real challenger below second
        place used to be invisible, because attribution compared rank 1 to
        rank 2 only."""
        request = make_request(needed_position=Position.FORWARD, absent_skill=4)
        pool = [
            make_candidate("perfect_skill", primary_position=Position.DEFENSE,
                           skill=4),
            make_candidate("zed_forward", primary_position=Position.FORWARD,
                           skill=5),
            make_candidate("abe_forward", primary_position=Position.FORWARD,
                           skill=7),
        ]
        kinds = (RuleKind.POSITION_PREFERENCE, RuleKind.SKILL_MATCH)
        result = rank_substitutes(request, pool, policy_of(*kinds))

        self.assertEqual(result.proposed_player_id, "zed_forward")
        self.assertEqual([e.player_id for e in result.ranked],
                         ["zed_forward", "abe_forward", "perfect_skill"])
        # Both rules are load-bearing, and the oracle proves it.
        self.assertEqual(result.proposal_deciding_rules, kinds)
        self.assertEqual(_winner_without(request, pool, kinds, 0, None),
                         "perfect_skill")
        self.assertEqual(_winner_without(request, pool, kinds, 1, None),
                         "abe_forward")
        # The captain's question "why not perfect_skill?" is answerable from
        # the line: it names POSITION_PREFERENCE and the player it displaced.
        self.assertIn("POSITION_PREFERENCE", result.decision_summary)
        self.assertIn("perfect_skill", result.decision_summary)
        self.assertIn("SKILL_MATCH", result.decision_summary)
        self.assertEqual(result.deciding_rule, RuleKind.POSITION_PREFERENCE)

    def test_overclaim_an_overdetermined_win_credits_no_rule(self):
        """Finding (2): the winner led on every rule, so no single rule was
        load-bearing. Naming one read causally and was false."""
        request = make_request(absent_skill=4)
        pool = [
            make_candidate("alpha", skill=4, completed_sub_games=0),
            make_candidate("bravo", skill=7, completed_sub_games=2),
        ]
        kinds = (RuleKind.SKILL_MATCH, RuleKind.FAIRNESS)
        result = rank_substitutes(request, pool, policy_of(*kinds))

        self.assertEqual(result.proposed_player_id, "alpha")
        # Oracle: deleting either rule still proposes alpha.
        self.assertEqual(_winner_without(request, pool, kinds, 0, None), "alpha")
        self.assertEqual(_winner_without(request, pool, kinds, 1, None), "alpha")
        self.assertEqual(result.proposal_deciding_rules, ())
        self.assertIsNone(result.deciding_rule)
        self.assertIn("no single rule decided it", result.decision_summary)
        self.assertNotIn("SKILL_MATCH", result.decision_summary)

    def test_override_credits_no_rule_for_the_selected_player(self):
        """Finding (3): with an override, FAIRNESS was credited for a player
        FAIRNESS ranked LAST. A human decided, so no rule decided."""
        request = make_request()
        pool = [
            make_candidate("player_free", completed_sub_games=0),
            make_candidate("player_busy", completed_sub_games=5),
        ]
        override = SelectionOverride(player_id="player_busy",
                                     actor_id="user_coach",
                                     reason="carpools with the captain")
        result = rank_substitutes(request, pool, policy_of(RuleKind.FAIRNESS),
                                  override=override)

        self.assertEqual(result.selected_player_id, "player_busy")
        self.assertIsNone(result.deciding_rule)
        # The proposal's own attribution survives, labelled as the proposal.
        self.assertEqual(result.proposed_player_id, "player_free")
        self.assertEqual(result.proposal_deciding_rules, (RuleKind.FAIRNESS,))
        self.assertTrue(result.decision_summary.startswith("player_busy selected:"))
        self.assertIn("engine proposal (not selected): player_free proposed:",
                      result.decision_summary)

    def test_sole_eligible_candidate_names_no_rule(self):
        """Degenerate case: with nobody to displace, no rule can be
        load-bearing — deleting any of them proposes the same player."""
        request = make_request()
        pool = [make_candidate("player_only"),
                make_candidate("player_g", primary_position=Position.GOALIE)]
        kinds = (RuleKind.FAIRNESS, RuleKind.SKILL_MATCH)
        result = rank_substitutes(request, pool, policy_of(*kinds))
        self.assertEqual(result.proposal_deciding_rules, ())
        self.assertIsNone(result.deciding_rule)
        self.assertEqual(result.decision_summary,
                         "player_only proposed: sole eligible candidate")
        for index in range(len(kinds)):
            self.assertEqual(_winner_without(request, pool, kinds, index, None),
                             "player_only")

    def test_an_exact_tie_all_the_way_through_names_no_rule(self):
        """Degenerate case: identical candidates, so only the player_id
        tiebreak separated them and every rule is inert."""
        request = make_request()
        pool = [make_candidate("player_z"), make_candidate("player_a")]
        kinds = (RuleKind.FAIRNESS, RuleKind.SKILL_MATCH,
                 RuleKind.POSITION_PREFERENCE)
        result = rank_substitutes(request, pool, policy_of(*kinds))
        self.assertEqual(result.proposed_player_id, "player_a")
        self.assertEqual(result.proposal_deciding_rules, ())
        self.assertIsNone(result.deciding_rule)
        self.assertIn("no rule separated them", result.decision_summary)
        for index in range(len(kinds)):
            self.assertEqual(_winner_without(request, pool, kinds, index, None),
                             "player_a")

    def test_a_winner_decided_only_by_the_random_tiebreak_names_random(self):
        """Degenerate case: everything ties, the seeded draw picks someone the
        player_id tiebreak would NOT have picked — so RANDOM is load-bearing."""
        request = make_request()
        pool = [make_candidate(f"player_{suffix}") for suffix in "abcd"]
        kinds = (RuleKind.FAIRNESS, RuleKind.RANDOM)
        result = rank_substitutes(request, pool,
                                  policy_of(*kinds, random_seed=2))
        self.assertEqual(result.proposed_player_id, "player_d")
        self.assertEqual(result.proposal_deciding_rules, (RuleKind.RANDOM,))
        self.assertEqual(result.deciding_rule, RuleKind.RANDOM)
        self.assertEqual(_winner_without(request, pool, kinds, 1, 2), "player_a")
        self.assertIn("RANDOM", result.decision_summary)
        self.assertIn("player_a", result.decision_summary)

    def test_a_random_draw_that_agrees_with_the_id_tiebreak_names_nothing(self):
        """Mirror of the above: with seed 1 the draw picks the player the id
        tiebreak would have picked anyway, so RANDOM is NOT load-bearing and
        must not be credited."""
        request = make_request()
        pool = [make_candidate(f"player_{suffix}") for suffix in "abcd"]
        kinds = (RuleKind.FAIRNESS, RuleKind.RANDOM)
        result = rank_substitutes(request, pool,
                                  policy_of(*kinds, random_seed=1))
        self.assertEqual(result.proposed_player_id, "player_a")
        self.assertEqual(_winner_without(request, pool, kinds, 1, 1), "player_a")
        self.assertEqual(result.proposal_deciding_rules, ())
        self.assertIsNone(result.deciding_rule)


class DecidingRuleInvariantTest(unittest.TestCase):
    """The definition holds over a swept space, not just on hand-picked pools."""

    RULE_SETS = [
        (RuleKind.FAIRNESS,),
        (RuleKind.SKILL_MATCH,),
        (RuleKind.FAIRNESS, RuleKind.SKILL_MATCH),
        (RuleKind.SKILL_MATCH, RuleKind.FAIRNESS),
        (RuleKind.POSITION_PREFERENCE, RuleKind.SKILL_MATCH),
        (RuleKind.POSITION_PREFERENCE, RuleKind.SKILL_MATCH, RuleKind.FAIRNESS),
        (RuleKind.FAIRNESS, RuleKind.RANDOM),
        (RuleKind.SKILL_MATCH, RuleKind.POSITION_PREFERENCE, RuleKind.RANDOM),
    ]
    SEED = 31337

    def _pools(self):
        """Deterministic, seeded pools — reproducible without a live RNG."""
        import random

        rng = random.Random(self.SEED)
        positions = [Position.FORWARD, Position.DEFENSE, Position.SKATER,
                     Position.GOALIE]
        for _ in range(300):
            size = rng.randint(1, 5)
            pool = [
                make_candidate(
                    f"player_{index}",
                    primary_position=rng.choice(positions),
                    skill=rng.choice([None, 1, 3, 4, 5, 7]),
                    completed_sub_games=rng.choice([0, 0, 1, 2, 5]),
                    min_notice_minutes=rng.choice([0, 0, 60, 100000]),
                )
                for index in range(size)
            ]
            yield rng.choice(positions), pool

    def test_named_rules_are_exactly_the_counterfactually_load_bearing_ones(self):
        checked_with_a_named_rule = 0
        for needed, pool in self._pools():
            request = make_request(needed_position=needed, absent_skill=4)
            for kinds in self.RULE_SETS:
                policy = policy_of(*kinds, random_seed=self.SEED)
                result = rank_substitutes(request, pool, policy)
                expected = tuple(
                    kind
                    for index, kind in enumerate(kinds)
                    if _winner_without(request, pool, kinds, index, self.SEED)
                    != result.proposed_player_id
                )
                context = f"needed={needed} kinds={kinds} pool={pool}"
                self.assertEqual(result.proposal_deciding_rules, expected,
                                 msg=context)
                # deciding_rule is the highest-priority one, or None.
                self.assertEqual(
                    result.deciding_rule,
                    expected[0] if expected else None, msg=context)
                if expected:
                    checked_with_a_named_rule += 1
        self.assertGreater(checked_with_a_named_rule, 100)

    def test_every_named_rule_is_quoted_in_the_summary_with_both_sides(self):
        """The line must carry, per named rule, the winner's value AND the
        displaced player's value for that same rule — not merely a rule name."""
        checked = 0
        for needed, pool in self._pools():
            request = make_request(needed_position=needed, absent_skill=4)
            for kinds in self.RULE_SETS:
                result = rank_substitutes(
                    request, pool, policy_of(*kinds, random_seed=self.SEED))
                if not result.proposal_deciding_rules:
                    continue
                winner = result.ranked[0]
                by_id = {e.player_id: e for e in result.ranked}
                summary = result.decision_summary
                context = f"needed={needed} kinds={kinds} summary={summary}"
                self.assertTrue(summary.startswith(f"{winner.player_id} proposed:"),
                                msg=context)
                for rule in result.proposal_deciding_rules:
                    index = kinds.index(rule)
                    self.assertIn(rule.name, summary, msg=context)
                    self.assertIn(winner.rule_traces[index].detail, summary,
                                  msg=context)
                    # The displaced player and their value for that rule.
                    displaced = _winner_without(
                        request, pool, kinds, index, self.SEED)
                    self.assertIn(displaced, summary, msg=context)
                    self.assertIn(by_id[displaced].rule_traces[index].detail,
                                  summary, msg=context)
                    checked += 1
        self.assertGreater(checked, 100)

    def test_a_rule_is_never_credited_for_an_overridden_selection(self):
        """(selected_player_id, deciding_rule) is never a false pair."""
        checked = 0
        for needed, pool in self._pools():
            if len(pool) < 2:
                continue
            request = make_request(needed_position=needed, absent_skill=4)
            for kinds in self.RULE_SETS:
                policy = policy_of(*kinds, random_seed=self.SEED)
                baseline = rank_substitutes(request, pool, policy)
                if len(baseline.ranked) < 2:
                    continue
                target_id = baseline.ranked[-1].player_id
                override = SelectionOverride(player_id=target_id,
                                             actor_id="user_admin",
                                             reason="showed up at the rink")
                result = rank_substitutes(
                    request, pool, policy,
                    override=override)
                self.assertTrue(result.override_applied)
                self.assertEqual(result.selected_player_id, target_id)
                self.assertIsNone(result.deciding_rule)
                self.assertTrue(
                    result.decision_summary.startswith(
                        f"{target_id} selected:"),
                    msg=result.decision_summary)
                checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
