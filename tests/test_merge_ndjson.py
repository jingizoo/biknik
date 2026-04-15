"""Unit tests for ofam_asset_xfer.merge_ndjson."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

from ofam_asset_xfer.merge_ndjson import (
    collect_ndjson_files,
    merge,
    merge_ndjson_files,
    ndjson_to_csv,
)


def _write_day(base: Path, day: str, rows: list[dict], filename: str = "results.ndjson"):
    d = base / day
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    (d / filename).write_text(text)


class TestCollectNdjsonFiles:
    def test_finds_date_folders(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"a": 1}])
        _write_day(tmp_path, "2025-01-02", [{"a": 2}])
        files = collect_ndjson_files(tmp_path)
        assert len(files) == 2
        assert files[0].parent.name == "2025-01-01"
        assert files[1].parent.name == "2025-01-02"

    def test_ignores_non_date_folders(self, tmp_path):
        _write_day(tmp_path, "not-a-date", [{"a": 1}])
        _write_day(tmp_path, "2025-03-01", [{"a": 2}])
        files = collect_ndjson_files(tmp_path)
        assert len(files) == 1

    def test_filters_by_after_date(self, tmp_path):
        _write_day(tmp_path, "2024-01-01", [{"a": 1}])
        _write_day(tmp_path, "2025-06-01", [{"a": 2}])
        files = collect_ndjson_files(tmp_path, after=date(2025, 1, 1))
        assert len(files) == 1
        assert files[0].parent.name == "2025-06-01"

    def test_empty_dir(self, tmp_path):
        assert collect_ndjson_files(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert collect_ndjson_files(tmp_path / "nope") == []

    def test_finds_errors_ndjson(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"e": 1}], "errors.ndjson")
        files = collect_ndjson_files(tmp_path, filename="errors.ndjson")
        assert len(files) == 1


class TestMergeNdjsonFiles:
    def test_merges_multiple_files(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"a": 1}])
        _write_day(tmp_path, "2025-01-02", [{"a": 2}, {"a": 3}])
        files = collect_ndjson_files(tmp_path)
        merged = merge_ndjson_files(files)
        lines = merged.strip().split("\n")
        assert len(lines) == 3

    def test_empty_file_list(self):
        assert merge_ndjson_files([]) == ""


class TestNdjsonToCsv:
    def test_converts_to_csv(self):
        ndjson = '{"name":"Alice","age":30}\n{"name":"Bob","age":25}\n'
        csv_text = ndjson_to_csv(ndjson)
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "Alice"
        assert rows[1]["age"] == "25"

    def test_empty_input(self):
        assert ndjson_to_csv("") == ""
        assert ndjson_to_csv("   ") == ""


class TestMerge:
    def test_produces_all_files(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"x": 1}])
        _write_day(tmp_path, "2025-01-02", [{"x": 2}])
        stats = merge(str(tmp_path))

        all_file = tmp_path / "results_all.ndjson"
        assert all_file.exists()
        lines = all_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert stats["results"]["rows"] == 2
        assert stats["results"]["files"] == 2

    def test_produces_errors_all(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"e": 1}], "errors.ndjson")
        stats = merge(str(tmp_path))
        assert (tmp_path / "errors_all.ndjson").exists()
        assert stats["errors"]["rows"] == 1

    def test_days_filter(self, tmp_path):
        old = (date.today() - timedelta(days=200)).isoformat()
        recent = date.today().isoformat()
        _write_day(tmp_path, old, [{"old": True}])
        _write_day(tmp_path, recent, [{"recent": True}])

        stats = merge(str(tmp_path), days=30)
        lines = (tmp_path / "results_all.ndjson").read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["recent"] is True

    def test_csv_output(self, tmp_path):
        _write_day(tmp_path, "2025-01-01", [{"name": "Alice", "score": 100}])
        merge(str(tmp_path), write_csv=True)

        csv_file = tmp_path / "results_all.csv"
        assert csv_file.exists()
        reader = csv.DictReader(io.StringIO(csv_file.read_text()))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"

    def test_custom_output_dir(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write_day(src, "2025-01-01", [{"a": 1}])

        merge(str(src), out_dir=str(dst))
        assert (dst / "results_all.ndjson").exists()

    def test_empty_base_dir(self, tmp_path):
        stats = merge(str(tmp_path))
        assert stats["results"]["rows"] == 0
        # File exists but is empty
        assert (tmp_path / "results_all.ndjson").read_text() == ""


class TestLocalPublisherAutoMerge:
    """Verify that LocalResultPublisher auto-generates results_all.ndjson."""

    def test_publish_creates_merged_file(self, tmp_path):
        from ofam_asset_xfer.local_publisher import LocalResultPublisher

        pub = LocalResultPublisher(str(tmp_path))
        pub.publish({"dry_run": False}, [{"asset_number": "100", "status": "TRANSFERRED"}])

        all_file = tmp_path / "results_all.ndjson"
        assert all_file.exists()
        lines = all_file.read_text().strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["asset_number"] == "100"

    def test_two_runs_merged(self, tmp_path):
        from ofam_asset_xfer.local_publisher import LocalResultPublisher

        pub = LocalResultPublisher(str(tmp_path))
        pub.publish({"dry_run": False}, [{"asset_number": "100", "status": "TRANSFERRED"}])
        pub.publish({"dry_run": False}, [{"asset_number": "200", "status": "FAILED", "error": "e"}])

        lines = (tmp_path / "results_all.ndjson").read_text().strip().split("\n")
        assert len(lines) == 2
