"""Publish transfer results as NDJSON to local disk.

Same Tableau-friendly flat format as :mod:`gcs_publisher`, but writes to a
local directory instead of GCS.  Useful for testing the pipeline locally
before deploying to GCS.

Output structure::

    <out_dir>/YYYY-MM-DD/results.ndjson   (appended per run)
    <out_dir>/YYYY-MM-DD/errors.ndjson    (FAILED rows only, appended)

Old date-folders beyond ``retention_days`` are deleted on each publish.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from .gcs_publisher import flatten_results, to_ndjson
from .merge_ndjson import merge

log = logging.getLogger(__name__)


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
        """Flatten, append to daily NDJSON files on disk, and prune old data."""
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
