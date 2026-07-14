"""Stable import-matching keys for the full setup hierarchy (#174 PR E, slice 1).

Migration 020 adds an ``external_ref`` column to organizations, leagues, venues,
seasons, levels, and divisions — the schema foundation the client hierarchy
import needs to be idempotent across repeat uploads (match-and-update by code
rather than create-a-duplicate-by-name). This slice is schema + store only; the
import service that populates these codes is the next slice. These tests prove
the column round-trips through a durable store reopen and that a record is
findable by its code, for every one of the six entities.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import (
    Division, League, Program, Organization, Season, Venue,
)
from hockey_scheduler.store import InMemoryStore, SqlStore

# (dataclass, store collection accessor, add method, save method, id prefix).
# #233 C1b: the umbrella is Program (store all_programs/add_program/...); the
# grouping formerly named Level is now League (store all_leagues/add_league/...).
_ENTITIES = [
    ("organization", Organization, "all_organizations", "add_organization",
     "save_organization"),
    ("league", Program, "all_programs", "add_program", "save_program"),
    ("venue", Venue, "all_venues", "add_venue", "save_venue"),
    ("season", Season, "all_seasons", "add_season", "save_season"),
    ("level", League, "all_leagues", "add_league", "save_league"),
    ("division", Division, "all_divisions", "add_division", "save_division"),
]


def _make(cls, ref):
    """A minimal instance of ``cls`` carrying external_ref=``ref``, filling in
    the required positional fields each dataclass needs."""
    common = {"id": f"{cls.__name__.lower()}_1", "name": "Example",
              "external_ref": ref}
    if cls is Organization:
        return Organization(**common)
    if cls is Program:
        return Program(**common)
    if cls is Venue:
        return Venue(**common)
    if cls is Season:
        return Season(program_id="program_1", **common)
    if cls is League:  # competition grouping (formerly Level)
        return League(season_id="season_1", **common)
    if cls is Division:
        return Division(season_id="season_1", **common)
    raise AssertionError(cls)


class HierarchyExternalRefModelTest(unittest.TestCase):
    def test_every_hierarchy_entity_defaults_external_ref_to_none(self):
        # Additive + nullable: a manually-created record simply has no code.
        for _, cls, _all, _add, _save in _ENTITIES:
            obj = _make(cls, None)
            self.assertIsNone(obj.external_ref, cls.__name__)


class HierarchyExternalRefStoreTest(unittest.TestCase):
    def test_in_memory_store_round_trips_external_ref(self):
        store = InMemoryStore()
        for name, cls, all_attr, add_attr, _save in _ENTITIES:
            getattr(store, add_attr)(_make(cls, f"CODE-{name}"))
        for name, cls, all_attr, _add, _save in _ENTITIES:
            rows = getattr(store, all_attr)()
            self.assertEqual([r.external_ref for r in rows], [f"CODE-{name}"], name)

    def test_external_ref_survives_a_durable_store_reopen(self):
        # The product-critical property: a code written before a restart is
        # still on the record (and still queryable) after reopening the same DB.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)

        store = SqlStore(path)
        for name, cls, _all, add_attr, _save in _ENTITIES:
            getattr(store, add_attr)(_make(cls, f"CODE-{name}"))
        del store

        reopened = SqlStore(path)
        for name, cls, all_attr, _add, _save in _ENTITIES:
            rows = getattr(reopened, all_attr)()
            self.assertEqual(len(rows), 1, name)
            self.assertEqual(rows[0].external_ref, f"CODE-{name}", name)
            # Findable by code — the lookup the idempotent import will rely on.
            match = next((r for r in rows if r.external_ref == f"CODE-{name}"),
                         None)
            self.assertIsNotNone(match, name)

    def test_save_updates_external_ref_in_place(self):
        # The update path the import's match-and-update step needs: re-saving an
        # existing record with a (newly) set code persists without a duplicate.
        for name, cls, all_attr, add_attr, save_attr in _ENTITIES:
            store = InMemoryStore()
            obj = _make(cls, None)
            getattr(store, add_attr)(obj)
            obj.external_ref = f"CODE-{name}"
            getattr(store, save_attr)(obj)
            rows = getattr(store, all_attr)()
            self.assertEqual(len(rows), 1, name)
            self.assertEqual(rows[0].external_ref, f"CODE-{name}", name)


if __name__ == "__main__":
    unittest.main()
