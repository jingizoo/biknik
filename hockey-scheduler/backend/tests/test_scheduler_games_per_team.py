"""GUARANTEED GAMES PER TEAM — the pairing generator's properties (#375).

The operator-facing control is inverted here: instead of asking for `m`
meetings against every opponent and letting games-per-team fall out as
`m x (T-1)`, the operator asks for `G` guaranteed games and the per-opponent
count is derived:

    opponents = T - 1
    base      = G // opponents   # games against EVERY opponent
    rem       = G %  opponents   # this many opponents are played once more

These are PROPERTY tests over the whole matrix a real league can ask for
(T from 2 to 20, G from 1 to 30), not a handful of worked examples, because
the interesting failures live at the edges of `rem`: a construction that is
right whenever `rem == 0` (which includes every "nice" example anyone tries
by hand) can be silently wrong for every other G. The naive construction
this file was first run against -- ceil(G / opponents) whole round-robins,
truncated to the right TOTAL number of pairings -- gets the total exactly
right and the guarantee wrong, which is precisely why "the totals look
right" is not evidence and these properties are asserted per team.

The five properties, stated once:

 1. EXACT GUARANTEE. Every team appears in exactly G pairings. This is the
    whole point of the field's name; a schedule where some team plays G-1 is
    not a schedule with a guarantee, it is a schedule with a nice average.
 2. SPREAD. Any one team's per-opponent counts differ by at most 1, so the
    derived `base`/`rem` split is honoured rather than one opponent being
    played five times while another is played once.
 3. TOTAL. Exactly T x G / 2 pairings, since every pairing contributes 2 to
    the league-wide game count.
 4. DETERMINISM. Identical output across repeated calls, across input
    orderings, and across a FRESH PROCESS -- `round_robin_pairings` already
    promises this and `_draft_fingerprint` depends on it, so a construction
    that reached for a set, a dict ordering or a hash would break commit.
 5. HOME/AWAY. Each pair's orientation split is balanced within one game.

FEASIBILITY is a property of the arithmetic, not of the construction: every
game contributes 2 to the league-wide count, so T x G must be even. Both odd
is refused (see `GamesPerTeamValidationTest`), so the matrix below skips
those combinations rather than asserting nonsense about them.
"""

import random
import subprocess
import sys
import unittest

from helpers import BACKEND  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services.scheduler import (
    MAX_GAMES_PER_TEAM,
    _normalize_games_per_team,
    _require_feasible_games_per_team,
    games_per_team_pairings,
    games_per_team_residual_pairings,
    require_completable_games_per_team,
)

# The matrix every property below is asserted over. T stops at 20 because
# that is the owner's own largest worked example; G stops at 30 because it
# is comfortably past any real regular season while still running fast.
TEAM_COUNTS = range(2, 21)
GAME_COUNTS = range(1, 31)


def teams(count):
    """Stable, sorted-order-independent ids.

    Zero-padded so lexical order matches numeric order: the generator sorts
    its input, and an unpadded "t10" would sort before "t2" and quietly
    change which construction indices mean which team -- making a real
    ordering bug look like a naming artefact.
    """
    return [f"t{i:02d}" for i in range(count)]


def feasible(team_count, games):
    """`T x G` even. See the module docstring: this is arithmetic, not policy."""
    return (team_count * games) % 2 == 0


def per_team_counts(pairings):
    counts = {}
    for home, away in pairings:
        counts[home] = counts.get(home, 0) + 1
        counts[away] = counts.get(away, 0) + 1
    return counts


def per_opponent_counts(pairings):
    """`{team: {opponent: times_played}}` -- the spread property's raw data."""
    table = {}
    for home, away in pairings:
        table.setdefault(home, {})[away] = table.setdefault(
            home, {}).get(away, 0) + 1
        table.setdefault(away, {})[home] = table.setdefault(
            away, {}).get(home, 0) + 1
    return table


class GamesPerTeamGuaranteeTest(unittest.TestCase):
    """Property 1 -- the guarantee itself, over the whole matrix."""

    def test_every_team_plays_exactly_the_guaranteed_number(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    counts = per_team_counts(
                        games_per_team_pairings(ids, games))
                    # Asserted per team, not as a set of values: a team that
                    # appears in ZERO pairings is missing from `counts`
                    # entirely, and `set(counts.values()) == {G}` would not
                    # notice.
                    for tid in ids:
                        self.assertEqual(
                            counts.get(tid, 0), games,
                            f"T={team_count} G={games}: {tid} plays "
                            f"{counts.get(tid, 0)}, not {games}")

    def test_fewer_than_two_teams_yields_no_pairings(self):
        # Pre-existing `round_robin_pairings` behaviour, preserved: a
        # division with nobody to play has no schedule rather than an error,
        # and the `T-1` denominator below never divides by zero.
        for ids in ([], ["solo"]):
            for games in (1, 2, 10):
                self.assertEqual(games_per_team_pairings(ids, games), [])


class GamesPerTeamSpreadTest(unittest.TestCase):
    """Property 2 -- the derived per-opponent split is actually derived."""

    def test_per_opponent_counts_differ_by_at_most_one(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    table = per_opponent_counts(
                        games_per_team_pairings(ids, games))
                    for tid in ids:
                        row = table.get(tid, {})
                        # Every opponent must appear, even at base 0: an
                        # opponent played zero times is a 0 in the spread,
                        # and omitting it would hide the worst violations.
                        counts = [row.get(other, 0)
                                  for other in ids if other != tid]
                        self.assertLessEqual(
                            max(counts) - min(counts), 1,
                            f"T={team_count} G={games}: {tid} spread "
                            f"{min(counts)}..{max(counts)}")

    def test_spread_matches_the_stated_base_and_remainder(self):
        # Not merely "within one" but the SPECIFIC base/rem the issue
        # specifies: `rem` opponents at base+1 and the rest at base.
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                opponents = team_count - 1
                base, rem = divmod(games, opponents)
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    table = per_opponent_counts(
                        games_per_team_pairings(ids, games))
                    for tid in ids:
                        row = table.get(tid, {})
                        counts = [row.get(other, 0)
                                  for other in ids if other != tid]
                        self.assertEqual(
                            sorted(counts),
                            sorted([base + 1] * rem
                                   + [base] * (opponents - rem)),
                            f"T={team_count} G={games}: {tid}")


class GamesPerTeamTotalTest(unittest.TestCase):
    """Property 3 -- the league-wide count."""

    def test_total_pairings_is_teams_times_games_over_two(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    self.assertEqual(
                        len(games_per_team_pairings(teams(team_count), games)),
                        team_count * games // 2)


class GamesPerTeamDeterminismTest(unittest.TestCase):
    """Property 4 -- `_draft_fingerprint` depends on every part of this."""

    def test_repeated_calls_are_identical(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    self.assertEqual(games_per_team_pairings(ids, games),
                                     games_per_team_pairings(ids, games))

    def test_input_order_does_not_change_the_output(self):
        for team_count in TEAM_COUNTS:
            for games in (1, 2, 3, 7, 20, 30):
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    self.assertEqual(
                        games_per_team_pairings(list(reversed(ids)), games),
                        games_per_team_pairings(ids, games))

    def test_identical_in_a_fresh_process(self):
        """A separate interpreter, so PYTHONHASHSEED differs.

        In-process repetition cannot catch a construction that iterates a
        set or a dict keyed by team id: within one process those orders are
        stable, so the same wrong answer is produced twice and the two
        agree. A fresh process is what makes the determinism claim real --
        it is the same claim `round_robin_pairings` already makes, and
        `_draft_fingerprint` (and therefore every commit) rests on it.
        """
        cases = [(2, 20), (5, 20), (20, 20), (7, 13), (9, 4), (16, 30)]
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(BACKEND)!r})\n"
            "from hockey_scheduler.services.scheduler import "
            "games_per_team_pairings\n"
            f"cases = {cases!r}\n"
            "out = [games_per_team_pairings("
            "[f't{i:02d}' for i in range(t)], g) for t, g in cases]\n"
            "print(json.dumps(out))\n"
        )
        import json
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"})
        fresh = json.loads(proc.stdout)
        mine = [games_per_team_pairings(teams(t), g) for t, g in cases]
        self.assertEqual(fresh, [[list(p) for p in run] for run in mine])


class GamesPerTeamHomeAwayTest(unittest.TestCase):
    """Property 5 -- orientation is balanced AND decided deterministically."""

    def test_home_away_balanced_within_one_game_per_pair(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    oriented = {}
                    for home, away in games_per_team_pairings(
                            teams(team_count), games):
                        oriented[(home, away)] = oriented.get(
                            (home, away), 0) + 1
                    seen = set()
                    for (home, away), count in oriented.items():
                        pair = frozenset((home, away))
                        if pair in seen:
                            continue
                        seen.add(pair)
                        reverse = oriented.get((away, home), 0)
                        self.assertLessEqual(
                            abs(count - reverse), 1,
                            f"T={team_count} G={games}: {home}/{away} hosts "
                            f"{count} vs {reverse}")

    def test_even_total_meetings_for_a_pair_split_exactly_evenly(self):
        # The stronger half of the guarantee: "within one" is forced only by
        # an odd number of meetings between the two teams. An even number
        # must split exactly, or the balance is an accident.
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    oriented = {}
                    for home, away in games_per_team_pairings(
                            teams(team_count), games):
                        oriented[(home, away)] = oriented.get(
                            (home, away), 0) + 1
                    for (home, away), count in oriented.items():
                        reverse = oriented.get((away, home), 0)
                        if (count + reverse) % 2 == 0:
                            self.assertEqual(
                                count, reverse,
                                f"T={team_count} G={games}: {home}/{away} "
                                f"meet {count + reverse} times but host "
                                f"{count}/{reverse}")


class GamesPerTeamWorkedExamplesTest(unittest.TestCase):
    """The owner's three worked examples, asserted the way they were stated."""

    def test_two_teams_twenty_games_meet_twenty_times(self):
        pairings = games_per_team_pairings(teams(2), 20)
        self.assertEqual(len(pairings), 20)  # 20 games total
        self.assertEqual({frozenset(p) for p in pairings},
                         {frozenset(("t00", "t01"))})
        self.assertEqual(set(per_team_counts(pairings).values()), {20})

    def test_five_teams_twenty_games_is_five_meetings_and_fifty_games(self):
        # 20 // 4 = 5, rem 0; every pair meets 5x; 50 games total.
        pairings = games_per_team_pairings(teams(5), 20)
        self.assertEqual(len(pairings), 50)
        table = per_opponent_counts(pairings)
        self.assertEqual(len(table), 5)
        for row in table.values():
            self.assertEqual(set(row.values()), {5})
        self.assertEqual(set(per_team_counts(pairings).values()), {20})

    def test_twenty_teams_twenty_games_is_once_each_plus_one_extra(self):
        # 20 // 19 = 1, rem 1; everyone meets once (19 games) plus one extra
        # each (10 more games); 200 games total.
        pairings = games_per_team_pairings(teams(20), 20)
        self.assertEqual(len(pairings), 200)
        self.assertEqual(set(per_team_counts(pairings).values()), {20})
        table = per_opponent_counts(pairings)
        for tid, row in table.items():
            # 18 opponents once, exactly one opponent twice.
            self.assertEqual(sorted(row.values()), [1] * 18 + [2], tid)
        # The extras form a perfect matching: exactly 10 doubled pairs.
        doubled = {frozenset((h, a)) for h, a in pairings
                   if sum(1 for x in pairings
                          if frozenset(x) == frozenset((h, a))) == 2}
        self.assertEqual(len(doubled), 10)


class GamesPerTeamRegularityTest(unittest.TestCase):
    """The extras multigraph really is `rem`-regular.

    The construction's correctness rests on this and nothing else, so it is
    asserted directly rather than inferred from the guarantee above: strip
    the `base` complete round-robins off the output and what remains must
    give every single team exactly `rem` additional games.
    """

    def test_extras_are_rem_regular_on_every_feasible_shape(self):
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                opponents = team_count - 1
                base, rem = divmod(games, opponents)
                with self.subTest(teams=team_count, games=games,
                                  base=base, rem=rem):
                    ids = teams(team_count)
                    table = per_opponent_counts(
                        games_per_team_pairings(ids, games))
                    for tid in ids:
                        row = table.get(tid, {})
                        extras = sum(
                            row.get(other, 0) - base
                            for other in ids if other != tid)
                        self.assertEqual(
                            extras, rem,
                            f"T={team_count} G={games}: {tid} gains {extras} "
                            f"extra games, not {rem}")

    def test_odd_team_counts_only_ever_need_an_even_remainder(self):
        # Why the odd-T circulant construction is possible at all: T odd
        # forces G even (T x G must be even), and (T-1) is then even too, so
        # `rem` is even and can be built from `rem/2` symmetric chords.
        for team_count in TEAM_COUNTS:
            if team_count % 2 == 0:
                continue
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                rem = games % (team_count - 1)
                self.assertEqual(rem % 2, 0,
                                 f"T={team_count} G={games} rem={rem}")


class GamesPerTeamValidationTest(unittest.TestCase):
    """`_normalize_games_per_team` -- shape, bounds, and the parity refusal."""

    def test_none_means_not_specified(self):
        self.assertIsNone(_normalize_games_per_team(None))

    def test_non_integers_and_bools_are_refused(self):
        # Same shape as `_normalize_meetings`: `True` would otherwise mean
        # "1 game" and `False` "0", neither of which any caller intended.
        for bad in (True, False, 1.5, "3", [], {}, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _normalize_games_per_team(bad)

    def test_below_one_is_refused(self):
        for bad in (0, -1, -30):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _normalize_games_per_team(bad)

    def test_above_the_ceiling_is_refused(self):
        with self.assertRaises(ValidationError):
            _normalize_games_per_team(MAX_GAMES_PER_TEAM + 1)
        self.assertEqual(_normalize_games_per_team(MAX_GAMES_PER_TEAM),
                         MAX_GAMES_PER_TEAM)


class GamesPerTeamFeasibilityTest(unittest.TestCase):
    """`T x G` must be even, and the refusal has to be actionable."""

    def test_every_feasible_combination_is_accepted(self):
        # Anti-vacuity control for the refusals below: the guard must be
        # silent on everything the generator can actually build, or a
        # "refuses the impossible" test would also be satisfied by a
        # function that refuses everything.
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    _require_feasible_games_per_team(team_count, games)

    def test_both_odd_is_refused(self):
        refused = 0
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    with self.assertRaises(ValidationError):
                        _require_feasible_games_per_team(team_count, games)
                refused += 1
        self.assertEqual(refused, 9 * 15)  # 9 odd T in 2..20, 15 odd G in 1..30

    def test_refusal_names_the_nearest_achievable_counts(self):
        with self.assertRaises(ValidationError) as caught:
            _require_feasible_games_per_team(5, 21)
        self.assertEqual(caught.exception.details["reason"],
                         "games_per_team_infeasible")
        self.assertEqual(caught.exception.details["nearest_achievable"],
                         [20, 22])
        self.assertIn("20 or 22", str(caught.exception))
        # And it says WHY, not just that it failed.
        self.assertIn("5 x 21 = 105", str(caught.exception))

    def test_nearest_achievable_never_suggests_a_refused_value(self):
        # G-1 = 0 is below the floor and G+1 is past the ceiling; neither
        # may be offered as advice the backend would then reject.
        low = None
        try:
            _require_feasible_games_per_team(3, 1)
        except ValidationError as exc:
            low = exc.details["nearest_achievable"]
        self.assertEqual(low, [2])
        high = None
        try:
            _require_feasible_games_per_team(3, MAX_GAMES_PER_TEAM)
        except ValidationError as exc:
            high = exc.details["nearest_achievable"]
        # MAX is even in this build, so pick the odd one below it.
        if MAX_GAMES_PER_TEAM % 2 == 0:
            self.assertIsNone(high)
            try:
                _require_feasible_games_per_team(3, MAX_GAMES_PER_TEAM - 1)
            except ValidationError as exc:
                high = exc.details["nearest_achievable"]
        self.assertEqual(high, [MAX_GAMES_PER_TEAM - 2, MAX_GAMES_PER_TEAM])

    def test_a_group_too_small_to_play_is_not_refused(self):
        # A Division with 0 or 1 teams produces no pairings at all (the
        # pre-existing `round_robin_pairings` answer). Refusing it on parity
        # would make one empty Division veto a whole League-wide draft.
        for team_count in (0, 1):
            for games in (1, 3, 21):
                _require_feasible_games_per_team(team_count, games)

    def test_refusal_can_name_the_group_that_cannot_honour_it(self):
        with self.assertRaises(ValidationError) as caught:
            _require_feasible_games_per_team(5, 21, label="Gold")
        self.assertIn("in Gold", str(caught.exception))


class ResidualCompletionPropertyTest(unittest.TestCase):
    """The residual planner's properties, over a matrix of FIXED graphs.

    The guarantee is the operator's final season total, so the generator's
    real question is not "what does a season look like from nothing?" but
    "what is still missing, given what is already on the calendar?". The
    three conditions `require_completable_games_per_team` enforces are
    claimed to be NECESSARY AND SUFFICIENT for that completion to exist, and
    a claim of sufficiency is exactly the kind that a handful of worked
    examples cannot support: a planner that refused every non-empty fixed
    graph would satisfy "never produces a wrong schedule" perfectly.

    So both directions are asserted over the same matrix:

    * whenever the conditions HOLD, a completion must be produced, and every
      team's fixed + generated total must be exactly G — no false refusals,
      no silently uneven schedule;
    * whenever they FAIL, the request must be refused — and independently of
      the planner's own opinion, since the expectation is computed here from
      the degree sequence rather than read back off the code under test.

    The fixed graphs are drawn from a SEEDED generator, so the matrix is the
    same on every machine, every backend and every run — the same
    determinism `_draft_fingerprint` depends on.
    """

    SEED = 20260805

    def fixed_graphs(self):
        """(teams, G, fixed multigraph) triples — deterministic, and covering
        the shapes that matter: the empty graph, a prefix of the canonical
        target (which the planner completes along its own plan), and graphs
        with SURPLUS on some pair (which force the degree-constrained
        completion instead)."""
        rng = random.Random(self.SEED)
        for team_count in range(2, 9):
            ids = teams(team_count)
            pairs = [(ids[i], ids[j])
                     for i in range(team_count)
                     for j in range(i + 1, team_count)]
            for games in range(1, 13):
                if not feasible(team_count, games):
                    continue
                target = games_per_team_pairings(ids, games)
                for trial in range(6):
                    fixed = {}
                    if trial == 0:
                        pass                      # empty calendar
                    elif trial == 1:              # a prefix of the target
                        for home, away in target[:len(target) // 2]:
                            key = tuple(sorted((home, away)))
                            fixed[key] = fixed.get(key, 0) + 1
                    else:                         # arbitrary, surplus included
                        for _ in range(rng.randrange(1, 2 * len(pairs) + 1)):
                            key = pairs[rng.randrange(len(pairs))]
                            fixed[key] = fixed.get(key, 0) + 1
                    yield ids, games, fixed

    def expected_to_be_completable(self, ids, games, fixed):
        """The three conditions, computed HERE from the degree sequence, so
        the test's expectation never comes from the code it is judging."""
        degree = {t: 0 for t in ids}
        for (low, high), count in fixed.items():
            degree[low] += count
            degree[high] += count
        residual = [games - degree[t] for t in ids]
        if any(r < 0 for r in residual):
            return False
        if sum(residual) % 2:
            return False
        return all(2 * r <= sum(residual) for r in residual)

    def test_completable_iff_the_three_conditions_hold(self):
        completed = refused = 0
        for ids, games, fixed in self.fixed_graphs():
            expected = self.expected_to_be_completable(ids, games, fixed)
            with self.subTest(teams=len(ids), games=games, fixed=sorted(fixed)):
                if not expected:
                    with self.assertRaises(ValidationError):
                        require_completable_games_per_team(ids, games, fixed)
                    refused += 1
                    continue
                require_completable_games_per_team(ids, games, fixed)
                pairings = games_per_team_residual_pairings(ids, games, fixed)
                counts = {t: 0 for t in ids}
                for (low, high), count in fixed.items():
                    counts[low] += count
                    counts[high] += count
                for home, away in pairings:
                    self.assertNotEqual(home, away, "a team cannot play itself")
                    counts[home] += 1
                    counts[away] += 1
                for tid in ids:
                    self.assertEqual(
                        counts[tid], games,
                        f"T={len(ids)} G={games}: {tid} finishes on "
                        f"{counts[tid]}, not {games}")
                completed += 1
        # Anti-vacuity: both branches must actually have been exercised, or
        # "refuses the impossible" is satisfied by refusing everything and
        # "completes the possible" by a matrix that is all empty graphs.
        self.assertGreater(completed, 200, "too few completable cases tried")
        self.assertGreater(refused, 50, "too few infeasible cases tried")

    def test_the_empty_calendar_is_the_plain_generator(self):
        """The residual planner subsumes `games_per_team_pairings` rather
        than sitting beside it: with nothing fixed the two must agree row for
        row AND orientation for orientation, which is what keeps every
        property above, every stored fingerprint and every contract test
        true. Asserted over the whole matrix, not spot-checked."""
        for team_count in TEAM_COUNTS:
            for games in GAME_COUNTS:
                if not feasible(team_count, games):
                    continue
                with self.subTest(teams=team_count, games=games):
                    ids = teams(team_count)
                    self.assertEqual(
                        games_per_team_residual_pairings(ids, games, {}, {}),
                        games_per_team_pairings(ids, games))

    def test_home_away_repairs_an_existing_imbalance(self):
        """The home/away half of the residual: a pair whose fixed Games are
        lopsided must have the remaining meetings handed to the other side,
        not have the imbalance compounded by a fixed alternation."""
        ids = teams(2)
        low, high = ids
        for fixed_home_for_high in range(1, 4):
            with self.subTest(reverse_games=fixed_home_for_high):
                games = 6
                fixed = {(low, high): fixed_home_for_high}
                hosted = {(low, high): [0, fixed_home_for_high]}
                pairings = games_per_team_residual_pairings(
                    ids, games, fixed, hosted)
                homes = {low: 0, high: fixed_home_for_high}
                for home, _away in pairings:
                    homes[home] += 1
                self.assertEqual(
                    homes[low], homes[high],
                    f"{fixed_home_for_high} reverse games then "
                    f"{len(pairings)} more should finish level: {homes}")

    def test_the_completion_is_stable_across_a_fresh_interpreter(self):
        """Determinism the way `_draft_fingerprint` needs it. Within one
        process a set- or dict-ordered construction produces the same wrong
        answer twice and the two agree, so this re-runs the completion in a
        SEPARATE interpreter and compares."""
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from hockey_scheduler.services.scheduler import "
            "games_per_team_residual_pairings as f\n"
            "ids = ['t%%02d' %% i for i in range(6)]\n"
            "fixed = {('t00','t03'): 3, ('t01','t02'): 1, ('t04','t05'): 2}\n"
            "print(f(ids, 8, fixed))\n" % str(BACKEND))
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, check=True)
        ids = teams(6)
        fixed = {("t00", "t03"): 3, ("t01", "t02"): 1, ("t04", "t05"): 2}
        self.assertEqual(
            out.stdout.strip(),
            repr(games_per_team_residual_pairings(ids, 8, fixed)))


if __name__ == "__main__":
    unittest.main()
