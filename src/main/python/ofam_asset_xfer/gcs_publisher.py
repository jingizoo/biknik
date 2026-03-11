"""Publish transfer results to GCS in a Tableau-friendly NDJSON format.

Each run **appends** flat rows to daily files::

    gs://<bucket>/<prefix>/YYYY-MM-DD/results.ndjson
    gs://<bucket>/<prefix>/YYYY-MM-DD/errors.ndjson   (FAILED rows only)

Every row is a self-contained JSON object with ``run_date``, ``run_ts``,
and ``dry_run`` stamped in, so Tableau / BigQuery can query without joins.

On each publish the publisher also **prunes** blobs whose date-folder is
older than ``retention_days`` (default 365).

Configuration
-------------
::

    {
      "gcs": {
        "bucket": "my-fa-transfer-logs",
        "prefix": "transfers",
        "service_account_file": "/path/to/sa-key.json",
        "service_account_file_env": "GCS_SA_KEY_PATH",
        "retention_days": 365
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List

from .exceptions import ConfigError

log = logging.getLogger(__name__)

# Matches date-named folders like  transfers/2025-03-09/
_DATE_FOLDER_RE = re.compile(r"(\d{4}-\d{2}-\d{2})/")


def _flatten_result(
    row: Dict[str, Any],
    run_date: str,
    run_ts: int,
    dry_run: bool,
) -> Dict[str, Any]:
    """Flatten a TransferResult dict into a Tableau-friendly row.

    Removes nested ``fusion_response`` (too large / variable for dashboards)
    and stamps run metadata onto every row.
    """
    flat: Dict[str, Any] = {
        "run_date": run_date,
        "run_ts": run_ts,
        "dry_run": dry_run,
        "asset_number": row.get("asset_number"),
        "status": row.get("status"),
        "source_book": row.get("source_book"),
        "target_book": row.get("target_book"),
        "transfer_to_entity": row.get("transfer_to_entity"),
        "transfer_date": row.get("transfer_date"),
        "error": row.get("error"),
    }
    return flat


def flatten_results(
    results: List[Dict[str, Any]],
    run_date: str,
    run_ts: int,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Convert raw results into flat Tableau-ready rows."""
    return [_flatten_result(r, run_date, run_ts, dry_run) for r in results]


def to_ndjson(rows: List[Dict[str, Any]]) -> str:
    """Serialise rows as newline-delimited JSON (one JSON object per line)."""
    return "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GCSPublisherConfig:
    """Immutable config for :class:`GCSResultPublisher`."""

    bucket: str
    prefix: str
    service_account_file: str
    retention_days: int = 365

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GCSPublisherConfig":
        """Build a GCSPublisherConfig from a raw config dict."""
        bucket = d.get("bucket", "").strip()
        if not bucket:
            raise ConfigError("gcs.bucket is required")
        # Strip gs:// scheme if provided (users often copy the full URI).
        if bucket.startswith("gs://"):
            bucket = bucket[len("gs://"):]
        bucket = bucket.strip("/")

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
        # Expand ~ to the user's home directory.
        sa_file = os.path.expanduser(sa_file)

        retention_days = int(d.get("retention_days", 365))

        return cls(
            bucket=bucket,
            prefix=prefix,
            service_account_file=sa_file,
            retention_days=retention_days,
        )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
class GCSResultPublisher:
    """Appends transfer results as NDJSON to daily files on GCS.

    Implements the :class:`~ofam_asset_xfer.result_publisher.ResultPublisher`
    protocol — no base-class coupling.
    """

    def __init__(self, config: GCSPublisherConfig) -> None:
        self._cfg = config
        self._client = None  # lazy

    def _get_client(self) -> Any:
        """Lazy-init the GCS client so import-time doesn't require google libs."""
        if self._client is None:
            from google.cloud import storage  # type: ignore[import-untyped]

            self._client = storage.Client.from_service_account_json(
                self._cfg.service_account_file
            )
        return self._client

    # -- append -------------------------------------------------------------

    def _append_ndjson(self, blob_path: str, ndjson_text: str) -> str:
        """Append *ndjson_text* to an existing blob, or create it."""
        client = self._get_client()
        bucket = client.bucket(self._cfg.bucket)
        blob = bucket.blob(blob_path)

        existing = ""
        if blob.exists():
            existing = blob.download_as_text(encoding="utf-8")

        merged = existing + ndjson_text
        blob.upload_from_string(merged, content_type="application/x-ndjson")

        uri = f"gs://{self._cfg.bucket}/{blob_path}"
        log.info("Appended %d row(s) to %s", ndjson_text.count("\n"), uri)
        return uri

    # -- prune --------------------------------------------------------------

    def _prune_old_blobs(self) -> int:
        """Delete blobs in date-folders older than retention_days."""
        cutoff = date.today() - timedelta(days=self._cfg.retention_days)
        client = self._get_client()
        bucket = client.bucket(self._cfg.bucket)
        prefix = self._cfg.prefix + "/"

        deleted = 0
        for blob in bucket.list_blobs(prefix=prefix):
            match = _DATE_FOLDER_RE.search(blob.name[len(prefix):])
            if not match:
                continue
            try:
                folder_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if folder_date < cutoff:
                blob.delete()
                deleted += 1

        if deleted:
            log.info("Pruned %d blob(s) older than %s", deleted, cutoff.isoformat())
        return deleted

    # -- ResultPublisher protocol -------------------------------------------

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Flatten, append to daily NDJSON files, and prune old data."""
        run_date = date.today().isoformat()
        run_ts = int(time.time())
        dry_run = summary.get("dry_run", True)

        flat = flatten_results(results, run_date, run_ts, dry_run)
        day_prefix = f"{self._cfg.prefix}/{run_date}"

        if flat:
            self._append_ndjson(f"{day_prefix}/results.ndjson", to_ndjson(flat))

        errors = [r for r in flat if r.get("status") == "FAILED"]
        if errors:
            self._append_ndjson(f"{day_prefix}/errors.ndjson", to_ndjson(errors))
            log.warning("Published %d error(s)", len(errors))
        else:
            log.info("No errors to publish")

        try:
            self._prune_old_blobs()
        except Exception:
            log.exception("Prune failed (non-fatal)")
