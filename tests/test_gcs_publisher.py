"""Unit tests for ofam_asset_xfer.gcs_publisher.

All tests mock the google.cloud.storage client — no network calls.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import date

from ofam_asset_xfer.gcs_publisher import (
    GCSPublisherConfig,
    GCSResultPublisher,
    flatten_results,
    to_ndjson,
    _flatten_result,
)
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.result_publisher import ResultPublisher


# ---------------------------------------------------------------------------
# flatten / ndjson helpers
# ---------------------------------------------------------------------------


class TestFlattenResult:
    def test_stamps_run_metadata(self):
        row = {"asset_number": "100", "status": "TRANSFERRED", "source_book": "US"}
        flat = _flatten_result(row, "2025-06-15", 1718400000, False)
        assert flat["run_date"] == "2025-06-15"
        assert flat["run_ts"] == 1718400000
        assert flat["dry_run"] is False
        assert flat["asset_number"] == "100"

    def test_strips_fusion_response(self):
        row = {"asset_number": "100", "status": "OK", "fusion_response": {"big": "data"}}
        flat = _flatten_result(row, "2025-01-01", 0, True)
        assert "fusion_response" not in flat

    def test_missing_fields_become_none(self):
        flat = _flatten_result({}, "2025-01-01", 0, True)
        assert flat["asset_number"] is None
        assert flat["error"] is None


class TestFlattenResults:
    def test_flattens_list(self):
        rows = [
            {"asset_number": "100", "status": "TRANSFERRED"},
            {"asset_number": "200", "status": "FAILED", "error": "timeout"},
        ]
        flat = flatten_results(rows, "2025-06-15", 100, False)
        assert len(flat) == 2
        assert flat[0]["asset_number"] == "100"
        assert flat[1]["error"] == "timeout"
        assert all(r["run_date"] == "2025-06-15" for r in flat)


class TestToNdjson:
    def test_one_json_per_line(self):
        rows = [{"a": 1}, {"b": 2}]
        text = to_ndjson(rows)
        lines = text.strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}

    def test_empty_list(self):
        assert to_ndjson([]) == ""

    def test_each_line_ends_with_newline(self):
        text = to_ndjson([{"x": 1}])
        assert text.endswith("\n")
        assert "\n" in text


# ---------------------------------------------------------------------------
# GCSPublisherConfig
# ---------------------------------------------------------------------------


class TestGCSPublisherConfig:
    def test_from_dict_basic(self):
        cfg = GCSPublisherConfig.from_dict(
            {
                "bucket": "my-bucket",
                "prefix": "logs",
                "service_account_file": "/path/to/sa.json",
            }
        )
        assert cfg.bucket == "my-bucket"
        assert cfg.prefix == "logs"
        assert cfg.service_account_file == "/path/to/sa.json"
        assert cfg.retention_days == 365

    def test_from_dict_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_SA_KEY", "/secret/sa.json")
        cfg = GCSPublisherConfig.from_dict(
            {
                "bucket": "b",
                "service_account_file_env": "MY_SA_KEY",
            }
        )
        assert cfg.service_account_file == "/secret/sa.json"

    def test_from_dict_default_prefix(self):
        cfg = GCSPublisherConfig.from_dict(
            {"bucket": "b", "service_account_file": "/sa.json"}
        )
        assert cfg.prefix == "transfers"

    def test_from_dict_custom_retention(self):
        cfg = GCSPublisherConfig.from_dict(
            {"bucket": "b", "service_account_file": "/sa.json", "retention_days": 90}
        )
        assert cfg.retention_days == 90

    def test_from_dict_strips_slashes(self):
        cfg = GCSPublisherConfig.from_dict(
            {"bucket": "b", "prefix": "/foo/bar/", "service_account_file": "/sa.json"}
        )
        assert cfg.prefix == "foo/bar"

    def test_missing_bucket_raises(self):
        with pytest.raises(ConfigError, match="bucket"):
            GCSPublisherConfig.from_dict({"service_account_file": "/sa.json"})

    def test_missing_sa_raises(self):
        with pytest.raises(ConfigError, match="service_account_file"):
            GCSPublisherConfig.from_dict({"bucket": "b"})

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        with pytest.raises(ConfigError, match="service_account_file"):
            GCSPublisherConfig.from_dict(
                {"bucket": "b", "service_account_file_env": "NONEXISTENT_VAR"}
            )


# ---------------------------------------------------------------------------
# GCSResultPublisher
# ---------------------------------------------------------------------------


def _make_publisher(**overrides):
    defaults = dict(
        bucket="test-bucket",
        prefix="transfers",
        service_account_file="/tmp/sa.json",
        retention_days=365,
    )
    defaults.update(overrides)
    cfg = GCSPublisherConfig(**defaults)
    return GCSResultPublisher(cfg)


def _mock_gcs_client():
    """Return (mock_client, mock_bucket, mock_blob) wired together."""
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False
    mock_blob.name = ""
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_bucket.list_blobs.return_value = []
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    return mock_client, mock_bucket, mock_blob


class TestGCSResultPublisher:
    def test_implements_protocol(self):
        assert isinstance(_make_publisher(), ResultPublisher)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_publish_appends_results_ndjson(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        summary = {"dry_run": False}
        results = [{"asset_number": "100", "status": "TRANSFERRED"}]

        pub.publish(summary, results)

        # Should create results.ndjson (no errors.ndjson since no FAILED)
        blob_paths = [c.args[0] for c in mock_bucket.blob.call_args_list]
        assert any("results.ndjson" in p for p in blob_paths)
        assert not any("errors.ndjson" in p for p in blob_paths)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_publish_appends_errors_ndjson_when_failures(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        results = [
            {"asset_number": "100", "status": "FAILED", "error": "HTTP 500"},
            {"asset_number": "200", "status": "TRANSFERRED"},
        ]

        pub.publish({"dry_run": False}, results)

        blob_paths = [c.args[0] for c in mock_bucket.blob.call_args_list]
        assert any("results.ndjson" in p for p in blob_paths)
        assert any("errors.ndjson" in p for p in blob_paths)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_appends_to_existing_blob(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = '{"old":"row"}\n'
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({"dry_run": True}, [{"asset_number": "100", "status": "DRY_RUN"}])

        uploaded = mock_blob.upload_from_string.call_args_list[0].args[0]
        lines = uploaded.strip().split("\n")
        # First line is the old data, second is the new append
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"old": "row"}

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_blob_path_has_date_folder(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({"dry_run": True}, [{"asset_number": "1", "status": "DRY_RUN"}])

        blob_path = mock_bucket.blob.call_args_list[0].args[0]
        today = date.today().isoformat()
        assert f"transfers/{today}/results.ndjson" == blob_path

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_content_type_is_ndjson(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({"dry_run": True}, [{"asset_number": "1", "status": "DRY_RUN"}])

        upload_call = mock_blob.upload_from_string.call_args
        assert upload_call.kwargs["content_type"] == "application/x-ndjson"

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_uploaded_rows_are_flat(self, mock_get_client):
        mock_client, mock_bucket, mock_blob = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish(
            {"dry_run": False},
            [{"asset_number": "42", "status": "TRANSFERRED", "fusion_response": {"x": 1}}],
        )

        uploaded = mock_blob.upload_from_string.call_args_list[0].args[0]
        row = json.loads(uploaded.strip())
        assert "fusion_response" not in row
        assert row["asset_number"] == "42"
        assert row["run_date"] == date.today().isoformat()
        assert row["dry_run"] is False


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


class TestPruneOldBlobs:
    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_deletes_old_blobs(self, mock_get_client):
        mock_client, mock_bucket, _ = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        old_blob = MagicMock()
        old_blob.name = "transfers/2020-01-01/results.ndjson"
        recent_blob = MagicMock()
        recent_blob.name = f"transfers/{date.today().isoformat()}/results.ndjson"
        mock_bucket.list_blobs.return_value = [old_blob, recent_blob]

        pub = _make_publisher()
        deleted = pub._prune_old_blobs()

        assert deleted == 1
        old_blob.delete.assert_called_once()
        recent_blob.delete.assert_not_called()

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_does_not_delete_within_retention(self, mock_get_client):
        mock_client, mock_bucket, _ = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        recent_blob = MagicMock()
        recent_blob.name = f"transfers/{date.today().isoformat()}/results.ndjson"
        mock_bucket.list_blobs.return_value = [recent_blob]

        pub = _make_publisher()
        deleted = pub._prune_old_blobs()

        assert deleted == 0
        recent_blob.delete.assert_not_called()

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_skips_non_date_folders(self, mock_get_client):
        mock_client, mock_bucket, _ = _mock_gcs_client()
        mock_get_client.return_value = mock_client

        blob = MagicMock()
        blob.name = "transfers/some-other-file.txt"
        mock_bucket.list_blobs.return_value = [blob]

        pub = _make_publisher()
        deleted = pub._prune_old_blobs()

        assert deleted == 0
        blob.delete.assert_not_called()
