"""Upload mass-additions audit artefacts to Google Cloud Storage.

Uploads ``proposed_updates.csv``, ``exceptions.csv``, and
``audit.jsonl`` from the local out-dir to a date-partitioned path::

    gs://<bucket>/<prefix>/<YYYY-MM-DD>/<run_ts>/proposed_updates.csv
    gs://<bucket>/<prefix>/<YYYY-MM-DD>/<run_ts>/exceptions.csv
    gs://<bucket>/<prefix>/<YYYY-MM-DD>/<run_ts>/audit.jsonl

The mass-additions runner writes plain CSV / JSONL (no NDJSON / Tableau
flattening — that pattern only applies to the asset-xfer pipeline), so
the publisher is a focused file-uploader rather than a schema-aware
NDJSON sink.

Service-account credentials are sourced in this precedence order:
  1. ``service_account_json_env`` — env var holding the raw SA JSON
     (vault-injected, the prod path).
  2. ``service_account_file``     — explicit path to a SA JSON file.
  3. ``service_account_file_env`` — env var holding the path to a file.
  4. Application-default credentials.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable

from ofam_mass_additions.exceptions import ConfigError
from ofam_mass_additions.runner.live_run import LiveRunResult

log = logging.getLogger(__name__)


_DEFAULT_FILES: tuple[str, ...] = (
    "proposed_updates.csv",
    "exceptions.csv",
    "audit.jsonl",
)


@dataclass(frozen=True)
class GCSPublisherConfig:
    bucket: str
    prefix: str = ""
    service_account_json_env: str = ""
    service_account_file: str = ""
    service_account_file_env: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GCSPublisherConfig":
        bucket = str(d.get("bucket", "")).strip()
        if not bucket:
            raise ConfigError("gcs.bucket is required")
        return cls(
            bucket=bucket,
            prefix=str(d.get("prefix", "")).strip().strip("/"),
            service_account_json_env=str(d.get("service_account_json_env", "")).strip(),
            service_account_file=str(d.get("service_account_file", "")).strip(),
            service_account_file_env=str(d.get("service_account_file_env", "")).strip(),
        )


class GCSAuditPublisher:
    """Uploads mass-additions audit files to GCS, one folder per run."""

    def __init__(self, cfg: GCSPublisherConfig) -> None:
        self.cfg = cfg

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GCSAuditPublisher":
        return cls(GCSPublisherConfig.from_dict(d))

    def publish(
        self,
        result: LiveRunResult,
        files: Iterable[str] = _DEFAULT_FILES,
    ) -> list[str]:
        """Upload the on-disk artefacts for ``result`` to GCS.

        Returns the list of ``gs://`` URIs written.  Missing files are
        skipped silently (e.g. a clean run won't produce an
        ``exceptions.csv``, but writing zero-row CSVs is fine too).
        """
        client = _build_client(self.cfg)
        bucket = client.bucket(self.cfg.bucket)
        run_folder = self._run_folder(result)

        written: list[str] = []
        for fname in files:
            local_path = result.out_dir / fname
            if not local_path.exists():
                log.debug("GCS upload: skip missing %s", local_path)
                continue
            blob = bucket.blob(f"{run_folder}/{fname}")
            blob.upload_from_filename(str(local_path))
            uri = f"gs://{self.cfg.bucket}/{run_folder}/{fname}"
            log.info("GCS upload: %s -> %s", local_path, uri)
            written.append(uri)

        # Sidecar manifest for downstream tooling.
        manifest_uri = self._upload_manifest(bucket, run_folder, result, written)
        if manifest_uri:
            written.append(manifest_uri)

        return written

    def _run_folder(self, result: LiveRunResult) -> str:
        # Use run_ts when available for uniqueness (e.g. re-runs on the
        # same day land in distinct folders).
        ts_token = (result.run_ts or "run").replace(":", "").replace("-", "")
        prefix = self.cfg.prefix
        date = result.run_date or "unknown-date"
        return "/".join(p for p in (prefix, date, ts_token) if p)

    def _upload_manifest(
        self,
        bucket: Any,
        run_folder: str,
        result: LiveRunResult,
        files_written: list[str],
    ) -> str:
        body = {
            "run_date": result.run_date,
            "run_ts": result.run_ts,
            "dry_run": result.dry_run,
            "pilot_book": result.pilot_book,
            "pilot_region": result.pilot_region,
            "counts": result.counts,
            "files": files_written,
        }
        blob = bucket.blob(f"{run_folder}/manifest.json")
        blob.upload_from_string(
            json.dumps(body, indent=2, sort_keys=True),
            content_type="application/json",
        )
        uri = f"gs://{self.cfg.bucket}/{run_folder}/manifest.json"
        log.info("GCS upload: manifest -> %s", uri)
        return uri


def _build_client(cfg: GCSPublisherConfig) -> Any:
    """Build a ``google.cloud.storage.Client`` honouring credential precedence."""
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError as e:
        raise ConfigError(
            "google-cloud-storage is required for GCS publishing; "
            "install it or remove the 'gcs' config block."
        ) from e

    sa_json = _read_env(cfg.service_account_json_env)
    if sa_json:
        # Materialise to a temp file and point the SDK at it via env.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(sa_json)
            tmp_path = tmp.name
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
        log.debug("GCS auth via service_account_json_env (materialised to %s)", tmp_path)
        return storage.Client()

    sa_file = cfg.service_account_file or _read_env(cfg.service_account_file_env)
    if sa_file:
        return storage.Client.from_service_account_json(sa_file)

    log.debug("GCS auth via Application Default Credentials")
    return storage.Client()


def _read_env(var: str) -> str:
    if not var:
        return ""
    return os.environ.get(var, "").strip()
