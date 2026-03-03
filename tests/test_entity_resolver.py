"""Unit tests for ofam_asset_xfer.entity_resolver."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_basic(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        assert "US ENTITY" in r.entity_book_map

    def test_empty_map_raises(self):
        with pytest.raises(ValidationError, match="entity_book_map is required"):
            EntityBookResolver({})

    def test_keys_normalised_to_upper(self):
        r = EntityBookResolver({"  uK entity  ": "UK CORP BOOK"})
        assert r.resolve_target_book("UK Entity") == "UK CORP BOOK"


# ---------------------------------------------------------------------------
# resolve_target_book
# ---------------------------------------------------------------------------

class TestResolveTargetBook:
    def test_exact_match(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK", "UK Entity": "UK CORP BOOK"})
        assert r.resolve_target_book("US Entity") == "US CORP BOOK"

    def test_case_insensitive(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        assert r.resolve_target_book("us entity") == "US CORP BOOK"
        assert r.resolve_target_book("US ENTITY") == "US CORP BOOK"

    def test_strips_whitespace(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        assert r.resolve_target_book("  US Entity  ") == "US CORP BOOK"

    def test_unknown_entity_raises(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        with pytest.raises(ValidationError, match="No book mapping for entity 'JP Entity'"):
            r.resolve_target_book("JP Entity")

    def test_empty_entity_raises(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        with pytest.raises(ValidationError, match="Empty entity name"):
            r.resolve_target_book("")

    def test_whitespace_only_entity_raises(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        with pytest.raises(ValidationError, match="Empty entity name"):
            r.resolve_target_book("   ")


# ---------------------------------------------------------------------------
# entity_book_map property
# ---------------------------------------------------------------------------

class TestEntityBookMap:
    def test_returns_copy(self):
        r = EntityBookResolver({"US Entity": "US CORP BOOK"})
        m = r.entity_book_map
        m["HACKED"] = "SHOULD NOT APPEAR"
        assert "HACKED" not in r.entity_book_map


# ---------------------------------------------------------------------------
# from_json_file
# ---------------------------------------------------------------------------

class TestFromJsonFile:
    def test_loads_from_file(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text(json.dumps({"US Entity": "US CORP BOOK", "UK Entity": "UK CORP BOOK"}))

        r = EntityBookResolver.from_json_file(str(p))
        assert r.resolve_target_book("US Entity") == "US CORP BOOK"
        assert r.resolve_target_book("UK Entity") == "UK CORP BOOK"

    def test_invalid_json_type_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(["not", "a", "dict"]))

        with pytest.raises(ValidationError, match="must be a dict"):
            EntityBookResolver.from_json_file(str(p))


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_inline_map(self):
        cfg = {"entity_book_map": {"US Entity": "US CORP BOOK"}}
        r = EntityBookResolver.from_config(cfg)
        assert r.resolve_target_book("US Entity") == "US CORP BOOK"

    def test_file_path(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text(json.dumps({"JP Entity": "JP CORP BOOK"}))

        cfg = {"entity_book_map_file": str(p)}
        r = EntityBookResolver.from_config(cfg)
        assert r.resolve_target_book("JP Entity") == "JP CORP BOOK"

    def test_missing_map_raises(self):
        with pytest.raises(ValidationError, match="entity_book_map"):
            EntityBookResolver.from_config({})
