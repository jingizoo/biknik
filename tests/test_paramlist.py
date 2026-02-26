"""Unit tests for ofam_asset_xfer.paramlist — parse_rosetta quoted string handling."""
from __future__ import annotations

from ofam_asset_xfer.paramlist import parse_rosetta


class TestParseRosetta:
    def test_plain_values(self):
        assert parse_rosetta("626955,626956") == ["626955", "626956"]

    def test_single_value(self):
        assert parse_rosetta("626955") == ["626955"]

    def test_none(self):
        assert parse_rosetta(None) == []

    def test_empty_string(self):
        assert parse_rosetta("") == []

    def test_single_comma(self):
        assert parse_rosetta(",") == []

    def test_quoted_values(self):
        """Fusion may return rosetta strings wrapped in single quotes."""
        assert parse_rosetta("'626955,626956'") == ["626955", "626956"]

    def test_quoted_single_value(self):
        assert parse_rosetta("'626955'") == ["626955"]

    def test_quoted_with_escaped_quote(self):
        """Escaped quotes ('') inside should be preserved as '."""
        assert parse_rosetta("'O''Brien,Smith'") == ["O'Brien", "Smith"]

    def test_spaces_trimmed(self):
        assert parse_rosetta(" 100 , 200 , 300 ") == ["100", "200", "300"]

    def test_empties_dropped(self):
        assert parse_rosetta("100,,200") == ["100", "200"]

    def test_integer_input(self):
        assert parse_rosetta(626955) == ["626955"]
