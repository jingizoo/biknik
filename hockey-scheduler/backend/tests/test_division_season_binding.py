"""Exact Season+League Division write contract (#345 review, additive).

``create_division_under_league()`` accepts an optional ``season_id``: when a
permanent League participates in more than one Season, the caller can select
the EXACT ``LeagueSeason`` binding to create the Division against, instead of
always failing ``ambiguous_season_for_league``. Omitting ``season_id``
preserves today's sole-binding behavior exactly -- unchanged and re-asserted
here alongside the new cases.

Fixture note: every scenario here binds ONE permanent League to BOTH an
older Season and the active one. The distinct-League-per-Season shape used
by this codebase's other fixtures (e.g. the pre-existing
``test_unbind_vs_create_division_under_league_is_linearizable`` and
``setup-workflow-hub.js``) cannot exercise the ambiguous/exact-binding
branches at all -- there is never more than one binding to disambiguate.

Runs against Memory and Sql (SQLite locally, PostgreSQL in the postgres CI
job via TEST_DATABASE_URL), same convention as test_setup_deletion.py's
MemoryDeletionTest/DurableDeletionTest split.
"""
import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.errors import NotFoundError, ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore

ACTOR = "admin"


class DivisionSeasonBindingContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def _division_count(self):
        return len(self.store.all_divisions())

    def _audit_count(self):
        return len(self.store.all_setup_audit())

    def _two_season_league(self):
        """One Program, two Seasons, ONE permanent League bound to BOTH."""
        prog = self.api.create_program("Prog", "US", "UTC")
        s1 = self.api.create_season(prog["id"], "S1")
        s2 = self.api.create_season(prog["id"], "S2")
        league = self.api.create_league(s1["id"], "Shared League")
        self.api.setup.create_league_season(league["id"], s2["id"], actor_id=ACTOR)
        return prog, s1, s2, league

    # -- positive: exact binding ---------------------------------------------
    def test_season_id_persists_under_exact_binding(self):
        prog, s1, s2, league = self._two_season_league()
        div = self.api.setup.create_division_under_league(
            league["id"], "Gold", season_id=s2["id"], actor_id=ACTOR)
        ls = self.store.get_league_season(div.league_season_id)
        self.assertEqual(ls.season_id, s2["id"])
        self.assertEqual(ls.league_id, league["id"])
        # The OTHER binding (S1) is completely untouched -- no division there.
        ls1 = self.store.league_season_for(league["id"], s1["id"])
        self.assertIsNotNone(ls1)
        divs_s1 = [d for d in self.store.all_divisions()
                   if d.league_season_id == ls1.id]
        self.assertEqual(divs_s1, [])

    def test_response_key_set_is_unchanged(self):
        # The v2 response shape must not change just because season_id is now
        # accepted -- same DIVISION_KEYS whether it's supplied or omitted.
        prog, s1, s2, league = self._two_season_league()
        resp = self.api.create_division_v2(
            league["id"], "Gold", season_id=s2["id"], actor_id=ACTOR)
        self.assertEqual(set(resp),
                         {"id", "season_id", "league_id", "name", "age_group",
                          "external_ref"})
        self.assertEqual(resp["season_id"], s2["id"])
        self.assertEqual(resp["league_id"], league["id"])

    # -- ambiguous legacy request without season_id still fails -------------
    def test_ambiguous_without_season_id_still_fails(self):
        prog, s1, s2, league = self._two_season_league()
        before_div, before_aud = self._division_count(), self._audit_count()
        with self.assertRaises(ValidationError) as ctx:
            self.api.setup.create_division_under_league(
                league["id"], "Gold", actor_id=ACTOR)
        self.assertEqual(ctx.exception.details.get("reason"),
                         "ambiguous_season_for_league")
        self.assertEqual(self._division_count(), before_div)
        self.assertEqual(self._audit_count(), before_aud)

    # -- negative: League and Season belong to different Programs -----------
    def test_program_mismatch_fails_closed(self):
        prog, s1, s2, league = self._two_season_league()
        other_prog = self.api.create_program("Other Prog", "US", "UTC")
        other_season = self.api.create_season(other_prog["id"], "Other S")
        before_div, before_aud = self._division_count(), self._audit_count()
        with self.assertRaises(ValidationError) as ctx:
            self.api.setup.create_division_under_league(
                league["id"], "Gold", season_id=other_season["id"],
                actor_id=ACTOR)
        self.assertEqual(ctx.exception.details.get("reason"),
                         "league_season_program_mismatch")
        self.assertEqual(self._division_count(), before_div)
        self.assertEqual(self._audit_count(), before_aud)

    # -- negative: season_id does not exist -----------------------------------
    def test_nonexistent_season_not_found(self):
        prog, s1, s2, league = self._two_season_league()
        before_div, before_aud = self._division_count(), self._audit_count()
        with self.assertRaises(NotFoundError):
            self.api.setup.create_division_under_league(
                league["id"], "Gold", season_id="nope_1", actor_id=ACTOR)
        self.assertEqual(self._division_count(), before_div)
        self.assertEqual(self._audit_count(), before_aud)

    # -- negative: real Season, same Program, but never bound to this League
    def test_unbound_pair_fails_closed(self):
        prog, s1, s2, league = self._two_season_league()
        s3 = self.api.create_season(prog["id"], "S3")  # same Program, no binding
        before_div, before_aud = self._division_count(), self._audit_count()
        with self.assertRaises(ValidationError) as ctx:
            self.api.setup.create_division_under_league(
                league["id"], "Gold", season_id=s3["id"], actor_id=ACTOR)
        self.assertEqual(ctx.exception.details.get("reason"), "league_not_in_season")
        self.assertEqual(self._division_count(), before_div)
        self.assertEqual(self._audit_count(), before_aud)

    # -- negative: the exact binding exists but its Season is archived ------
    def test_archived_season_fails_closed(self):
        prog, s1, s2, league = self._two_season_league()
        self.api.setup.archive_season(s2["id"], actor_id=ACTOR, reason="done")
        before_div, before_aud = self._division_count(), self._audit_count()
        with self.assertRaises(ValidationError) as ctx:
            self.api.setup.create_division_under_league(
                league["id"], "Gold", season_id=s2["id"], actor_id=ACTOR)
        self.assertEqual(ctx.exception.details.get("reason"), "season_archived")
        self.assertEqual(self._division_count(), before_div)
        self.assertEqual(self._audit_count(), before_aud)

    # -- backward compatibility: single-binding League, no season_id --------
    def test_backward_compatible_single_binding_without_season_id(self):
        prog = self.api.create_program("Solo Prog", "US", "UTC")
        season = self.api.create_season(prog["id"], "Solo S")
        league = self.api.create_league(season["id"], "Solo League")
        div = self.api.setup.create_division_under_league(
            league["id"], "Gold", actor_id=ACTOR)
        ls = self.store.get_league_season(div.league_season_id)
        self.assertEqual(ls.season_id, season["id"])

    # -- backward compatibility: single-binding League, season_id supplied --
    # (a caller that always sends the active Season, even when the League
    # happens to have only one binding, must still succeed identically.)
    def test_backward_compatible_single_binding_with_season_id(self):
        prog = self.api.create_program("Solo Prog 2", "US", "UTC")
        season = self.api.create_season(prog["id"], "Solo S2")
        league = self.api.create_league(season["id"], "Solo League 2")
        div = self.api.setup.create_division_under_league(
            league["id"], "Gold", season_id=season["id"], actor_id=ACTOR)
        ls = self.store.get_league_season(div.league_season_id)
        self.assertEqual(ls.season_id, season["id"])

    # -- legacy POSITIONAL callers must keep working unchanged (review) -----
    # season_id was added AFTER actor_id (keyword-only) specifically so a
    # caller passing all four positional args -- (league_id, name, age_group,
    # actor_id), the shape every pre-existing caller in this repo already
    # used before this batch -- can never have its actor_id silently
    # reinterpreted as a Season id.
    def test_legacy_four_positional_args_setup_service(self):
        prog = self.api.create_program("Pos Prog", "US", "UTC")
        season = self.api.create_season(prog["id"], "Pos S")
        league = self.api.create_league(season["id"], "Pos League")
        div = self.api.setup.create_division_under_league(
            league["id"], "Gold", "U14", "positional-actor")
        ls = self.store.get_league_season(div.league_season_id)
        self.assertEqual(ls.season_id, season["id"])
        self.assertEqual(div.age_group, "U14")
        audits = [a for a in self.store.all_setup_audit()
                  if a.entity_id == div.id and a.action == "division_created"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].actor_id, "positional-actor")

    def test_legacy_four_positional_args_api_facade(self):
        prog = self.api.create_program("Pos Prog 2", "US", "UTC")
        season = self.api.create_season(prog["id"], "Pos S2")
        league = self.api.create_league(season["id"], "Pos League 2")
        resp = self.api.create_division_v2(
            league["id"], "Gold", "U16", "positional-actor-2")
        self.assertEqual(resp["season_id"], season["id"])
        audits = [a for a in self.store.all_setup_audit()
                  if a.entity_id == resp["id"] and a.action == "division_created"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].actor_id, "positional-actor-2")


class MemoryDivisionSeasonBindingTest(DivisionSeasonBindingContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableDivisionSeasonBindingTest(DivisionSeasonBindingContract, unittest.TestCase):
    def make_store(self):
        # Honor TEST_DATABASE_URL so the PostgreSQL CI job exercises these
        # against Postgres; fall back to SQLite otherwise.
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
