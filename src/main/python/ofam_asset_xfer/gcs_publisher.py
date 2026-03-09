"""Publish transfer results and error logs to a GCS bucket.

Uses a service-account JSON key file for authentication.  The publisher writes
three artefacts per run:

    gs://<bucket>/<prefix>/summary.json      – high-level counts & timing
    gs://<bucket>/<prefix>/results.json       – per-asset transfer outcomes
    gs://<bucket>/<prefix>/errors.json        – only the FAILED rows (convenience)

``prefix`` defaults to ``transfers/<YYYY-MM-DD>/<epoch>`` so each run gets its
own folder and nothing is overwritten.

Configuration
-------------
Add a ``gcs`` block to your config JSON::

    {
      "gcs": {
        "bucket": "my-fa-transfer-logs",
        "prefix": "transfers",
        "service_account_file": "/path/to/sa-key.json",
        "service_account_file_env": "GCS_SA_KEY_PATH"
      }
    }

Either ``service_account_file`` or ``service_account_file_env`` (env-var name
whose value is the path) must be provided.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timezone, datetime
from typing import Any, Dict, List, Optional

from .exceptions import ConfigError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GCSPublisherConfig:
    """Immutable config for :class:`GCSResultPublisher`."""

    bucket: str
    prefix: str
    service_account_file: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GCSPublisherConfig":
        bucket = d.get("bucket", "").strip()
        if not bucket:
            raise ConfigError("gcs.bucket is required")

        prefix = d.get("prefix", "transfers").strip().strip("/")

        sa_file = d.get("service_account_file", "").strip()
        if not sa_file:
            env_key = d.get("service_account_file_env", "").strip()
            if env_key:
                sa_file = os.environ.get(env_key, "").strip()
            if not sa_file:
                raise ConfigError(
                    "gcs.service_account_file or gcs.service_account_file_env is required"
                )

        return cls(bucket=bucket, prefix=prefix, service_account_file=sa_file)


class GCSResultPublisher:
    """Writes transfer results & error logs to a GCS bucket.

    Implements the :class:`~ofam_asset_xfer.result_publisher.ResultPublisher`
    protocol — no base-class coupling.
    """

    def __init__(self, config: GCSPublisherConfig) -> None:
        self._cfg = config
        self._client = None  # lazy

    def _get_client(self):
        """Lazy-init the GCS client so import-time doesn't require google libs."""
        if self._client is None:
            from google.cloud import storage  # type: ignore[import-untyped]

            self._client = storage.Client.from_service_account_json(
                self._cfg.service_account_file
            )
        return self._client

    def _run_prefix(self) -> str:
        today = date.today().isoformat()
        epoch = int(time.time())
        return f"{self._cfg.prefix}/{today}/{epoch}"

    def _upload_json(self, blob_path: str, obj: Any) -> str:
        client = self._get_client()
        bucket = client.bucket(self._cfg.bucket)
        blob = bucket.blob(blob_path)
        payload = json.dumps(obj, indent=2, sort_keys=True, default=str)
        blob.upload_from_string(payload, content_type="application/json")
        uri = f"gs://{self._cfg.bucket}/{blob_path}"
        log.info("Uploaded %s (%d bytes)", uri, len(payload))
        return uri

    # ---- ResultPublisher protocol ------------------------------------------

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Upload summary, full results, and an errors-only extract to GCS."""
        prefix = self._run_prefix()

        self._upload_json(f"{prefix}/summary.json", summary)
        self._upload_json(f"{prefix}/results.json", results)

        errors = [r for r in results if r.get("status") == "FAILED"]
        if errors:
            self._upload_json(f"{prefix}/errors.json", errors)
            log.warning(
                "Published %d error(s) to gs://%s/%s/errors.json",
                len(errors),
                self._cfg.bucket,
                prefix,
            )
        else:
            log.info("No errors to publish")
