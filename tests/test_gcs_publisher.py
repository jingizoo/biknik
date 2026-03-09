"""Unit tests for ofam_asset_xfer.gcs_publisher.

All tests mock the google.cloud.storage client — no network calls.
"""

from __future__ import annotations

import json
import os
import pytest
from unittest.mock import MagicMock, patch, call

from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.result_publisher import ResultPublisher


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
            {
                "bucket": "b",
                "service_account_file": "/sa.json",
            }
        )
        assert cfg.prefix == "transfers"

    def test_from_dict_strips_slashes(self):
        cfg = GCSPublisherConfig.from_dict(
            {
                "bucket": "b",
                "prefix": "/foo/bar/",
                "service_account_file": "/sa.json",
            }
        )
        assert cfg.prefix == "foo/bar"

    def test_missing_bucket_raises(self):
        with pytest.raises(ConfigError, match="bucket"):
            GCSPublisherConfig.from_dict(
                {"service_account_file": "/sa.json"}
            )

    def test_missing_sa_raises(self):
        with pytest.raises(ConfigError, match="service_account_file"):
            GCSPublisherConfig.from_dict({"bucket": "b"})

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        with pytest.raises(ConfigError, match="service_account_file"):
            GCSPublisherConfig.from_dict(
                {
                    "bucket": "b",
                    "service_account_file_env": "NONEXISTENT_VAR",
                }
            )


# ---------------------------------------------------------------------------
# GCSResultPublisher
# ---------------------------------------------------------------------------


def _make_publisher():
    cfg = GCSPublisherConfig(
        bucket="test-bucket",
        prefix="transfers",
        service_account_file="/tmp/sa.json",
    )
    return GCSResultPublisher(cfg)


class TestGCSResultPublisher:
    def test_implements_protocol(self):
        pub = _make_publisher()
        assert isinstance(pub, ResultPublisher)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_publish_uploads_summary_and_results(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        summary = {"counts": {"total": 2, "transferred": 1, "failed": 0}}
        results = [
            {"asset_number": "100", "status": "TRANSFERRED"},
            {"asset_number": "200", "status": "DRY_RUN"},
        ]

        pub.publish(summary, results)

        # Should upload summary.json and results.json (no errors.json since no FAILED)
        assert mock_bucket.blob.call_count == 2
        blob_paths = [c.args[0] for c in mock_bucket.blob.call_args_list]
        assert any("summary.json" in p for p in blob_paths)
        assert any("results.json" in p for p in blob_paths)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_publish_uploads_errors_when_failures_exist(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        summary = {"counts": {"total": 2, "transferred": 0, "failed": 2}}
        results = [
            {"asset_number": "100", "status": "FAILED", "error": "HTTP 500"},
            {"asset_number": "200", "status": "FAILED", "error": "timeout"},
        ]

        pub.publish(summary, results)

        # summary + results + errors = 3 uploads
        assert mock_bucket.blob.call_count == 3
        blob_paths = [c.args[0] for c in mock_bucket.blob.call_args_list]
        assert any("errors.json" in p for p in blob_paths)

        # Verify the third upload (errors.json) contains only FAILED rows
        # The upload order is: summary, results, errors
        errors_payload = mock_blob.upload_from_string.call_args_list[2].args[0]
        parsed = json.loads(errors_payload)
        assert len(parsed) == 2
        assert all(r["status"] == "FAILED" for r in parsed)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_publish_no_errors_file_when_all_pass(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({}, [{"asset_number": "100", "status": "TRANSFERRED"}])

        blob_paths = [c.args[0] for c in mock_bucket.blob.call_args_list]
        assert not any("errors.json" in p for p in blob_paths)

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_blob_path_contains_prefix_and_date(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({}, [])

        blob_path = mock_bucket.blob.call_args_list[0].args[0]
        assert blob_path.startswith("transfers/")
        # Should contain a date-like segment (YYYY-MM-DD)
        parts = blob_path.split("/")
        assert len(parts) >= 3

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_upload_content_type_is_json(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        pub = _make_publisher()
        pub.publish({"key": "val"}, [])

        upload_call = mock_blob.upload_from_string.call_args
        assert upload_call.kwargs["content_type"] == "application/json"

    @patch("ofam_asset_xfer.gcs_publisher.GCSResultPublisher._get_client")
    def test_uploaded_json_is_valid(self, mock_get_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        summary = {"counts": {"total": 1}, "books": ["US CORP BOOK"]}
        pub = _make_publisher()
        pub.publish(summary, [])

        payload = mock_blob.upload_from_string.call_args_list[0].args[0]
        parsed = json.loads(payload)
        assert parsed["counts"]["total"] == 1
