# =============================================================================
# MERGED FILE — contains 3 original modules, each section is self-contained.
#
#   Section 1 → result_publisher.py
#   Section 2 → gcs_publisher.py
#   Section 3 → local_publisher.py
#
# To find section boundaries:  grep -n "^# >>> FILE\|^# <<< END" publishers.py
#
# To split back, extract each section (excluding its header/footer marker
# lines) and save to the filename shown in the marker.
#
# One cross-import was neutralised for the merged file:
#   local_publisher.py line 26:
#     FROM:  from .gcs_publisher import flatten_results, to_ndjson
#     TO:    (functions are already defined above in Section 2)
#   When splitting, restore that import line in local_publisher.py.
# =============================================================================

from __future__ import annotations  # shared — keep one copy at top of file

# >>> FILE: result_publisher.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""Abstract result publisher protocol.

Decouples the sync engine from any specific output destination (GCS, BigQuery,
local disk, etc.).  The sync engine calls ``publish()`` with structured results;
the concrete implementation decides where they go.
"""

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class ResultPublisher(Protocol):
    """Publishes transfer results to an external sink."""

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Persist *summary* and per-asset *results* to the configured sink."""
        ...
# <<< END: result_publisher.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<


# >>> FILE: gcs_publisher.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
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

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

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

    def _get_client(self):
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
# <<< END: gcs_publisher.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<


# >>> FILE: local_publisher.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""Publish transfer results as NDJSON to local disk.

Same Tableau-friendly flat format as :mod:`gcs_publisher`, but writes to a
local directory instead of GCS.  Useful for testing the pipeline locally
before deploying to GCS.

Output structure::

    <out_dir>/YYYY-MM-DD/results.ndjson   (appended per run)
    <out_dir>/YYYY-MM-DD/errors.ndjson    (FAILED rows only, appended)

Old date-folders beyond ``retention_days`` are deleted on each publish.
"""

# NOTE (merged): the original file imported flatten_results and to_ndjson
# from .gcs_publisher — they are already defined above in Section 2.
# When splitting, restore this line at the top of local_publisher.py:
#   from .gcs_publisher import flatten_results, to_ndjson

import shutil
from pathlib import Path

from .merge_ndjson import merge


class LocalResultPublisher:
    """Appends NDJSON results to daily files on local disk.

    Implements the :class:`~ofam_asset_xfer.result_publisher.ResultPublisher`
    protocol.
    """

    def __init__(self, out_dir: str, retention_days: int = 365) -> None:
        self._out_dir = Path(out_dir)
        self._retention_days = retention_days

    def _append_ndjson(self, path: Path, ndjson_text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(ndjson_text)
        log.info("Appended %d row(s) to %s", ndjson_text.count("\n"), path)

    def _prune_old_dirs(self) -> int:
        cutoff = date.today() - timedelta(days=self._retention_days)
        deleted = 0
        if not self._out_dir.exists():
            return 0
        for child in self._out_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                folder_date = date.fromisoformat(child.name)
            except ValueError:
                continue
            if folder_date < cutoff:
                shutil.rmtree(child)
                deleted += 1
                log.info("Pruned old folder %s", child)
        return deleted

    # -- ResultPublisher protocol -------------------------------------------

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        run_date = date.today().isoformat()
        run_ts = int(time.time())
        dry_run = summary.get("dry_run", True)

        flat = flatten_results(results, run_date, run_ts, dry_run)
        day_dir = self._out_dir / run_date

        if flat:
            self._append_ndjson(day_dir / "results.ndjson", to_ndjson(flat))

        errors = [r for r in flat if r.get("status") == "FAILED"]
        if errors:
            self._append_ndjson(day_dir / "errors.ndjson", to_ndjson(errors))
            log.warning("Published %d error(s) to %s", len(errors), day_dir)
        else:
            log.info("No errors to publish")

        try:
            self._prune_old_dirs()
        except Exception:
            log.exception("Prune failed (non-fatal)")

        try:
            merge(str(self._out_dir))
        except Exception:
            log.exception("Merge failed (non-fatal)")
# <<< END: local_publisher.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
