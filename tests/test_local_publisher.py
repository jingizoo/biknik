"""Unit tests for ofam_asset_xfer.local_publisher."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from ofam_asset_xfer.local_publisher import LocalResultPublisher
from ofam_asset_xfer.result_publisher import ResultPublisher


class TestLocalResultPublisher:
    def test_implements_protocol(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        assert isinstance(pub, ResultPublisher)

    def test_creates_ndjson_files(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish(
            {"dry_run": False},
            [{"asset_number": "100", "status": "TRANSFERRED"}],
        )

        today = date.today().isoformat()
        results_file = tmp_path / today / "results.ndjson"
        assert results_file.exists()

        lines = results_file.read_text().strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["asset_number"] == "100"
        assert row["run_date"] == today
        assert row["dry_run"] is False

    def test_appends_on_second_run(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish({"dry_run": True}, [{"asset_number": "100", "status": "DRY_RUN"}])
        pub.publish({"dry_run": True}, [{"asset_number": "200", "status": "DRY_RUN"}])

        today = date.today().isoformat()
        lines = (tmp_path / today / "results.ndjson").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["asset_number"] == "100"
        assert json.loads(lines[1])["asset_number"] == "200"

    def test_errors_file_only_for_failures(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish(
            {"dry_run": False},
            [
                {"asset_number": "100", "status": "TRANSFERRED"},
                {"asset_number": "200", "status": "FAILED", "error": "timeout"},
            ],
        )

        today = date.today().isoformat()
        errors_file = tmp_path / today / "errors.ndjson"
        assert errors_file.exists()

        lines = errors_file.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["asset_number"] == "200"

    def test_no_errors_file_when_all_pass(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish(
            {"dry_run": False},
            [{"asset_number": "100", "status": "TRANSFERRED"}],
        )

        today = date.today().isoformat()
        assert not (tmp_path / today / "errors.ndjson").exists()

    def test_prunes_old_directories(self, tmp_path):
        # Create a dir from 2 years ago
        old_date = (date.today() - timedelta(days=400)).isoformat()
        old_dir = tmp_path / old_date
        old_dir.mkdir()
        (old_dir / "results.ndjson").write_text('{"old": true}\n')

        # Create a recent dir
        recent_date = date.today().isoformat()
        recent_dir = tmp_path / recent_date
        recent_dir.mkdir()
        (recent_dir / "results.ndjson").write_text('{"recent": true}\n')

        pub = LocalResultPublisher(str(tmp_path), retention_days=365)
        pub.publish({"dry_run": True}, [{"asset_number": "1", "status": "DRY_RUN"}])

        assert not old_dir.exists()
        assert recent_dir.exists()

    def test_empty_results_no_crash(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish({"dry_run": True}, [])

        today = date.today().isoformat()
        # No files created for empty results
        assert not (tmp_path / today / "results.ndjson").exists()

    def test_flat_row_excludes_fusion_response(self, tmp_path):
        pub = LocalResultPublisher(str(tmp_path))
        pub.publish(
            {"dry_run": False},
            [{"asset_number": "42", "status": "OK", "fusion_response": {"big": "data"}}],
        )

        today = date.today().isoformat()
        line = (tmp_path / today / "results.ndjson").read_text().strip()
        row = json.loads(line)
        assert "fusion_response" not in row
        assert row["asset_number"] == "42"
