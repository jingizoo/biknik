# =============================================================================
# MERGED FILE — contains 2 original modules, each section is self-contained.
#
#   Section 1 → gcs_publisher.py
#   Section 2 → job.py
#
# To find section boundaries:  grep -n "^# >>> FILE\|^# <<< END" gcs_job.py
#
# To split back, extract each section (excluding its header/footer marker
# lines) and save to the filename shown in the marker.
#
# One cross-import was neutralised for the merged file:
#   job.py line 13:
#     FROM:  from .gcs_publisher import GCSPublisherConfig, GCSResultPublisher
#     TO:    (classes are already defined above in Section 1)
#   When splitting, restore that import line in job.py.
# =============================================================================

from __future__ import annotations  # shared — keep one copy at top of file

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
from typing import Any, Dict, List, Optional

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
# <<< END: gcs_publisher.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<


# >>> FILE: job.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

# NOTE (merged): the original file imported GCSPublisherConfig and
# GCSResultPublisher from .gcs_publisher — they are already defined above
# in Section 1.  When splitting, restore this line at the top of job.py:
#   from .gcs_publisher import GCSPublisherConfig, GCSResultPublisher

from .bip_client import BIPClient, BIPConfig
from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError
from .fusion_sync import FusionIUSync, DFFConfig
from .oracle_client import OracleConfig, OracleErpIntegrationsClient
from .local_publisher import LocalResultPublisher
from .store import ArtifactStore
from .fusion_ops import (
    get_asset_information,
    build_same_book_transfer_params,
    build_same_book_transfer_params_option_b,
    build_book_transfer_params,
    build_add_asset_params,
    build_retire_asset_params,
)
from .template import render


def _load_config(config_uri: str) -> Dict[str, Any]:
    store = ArtifactStore(base_uri=".")
    try:
        raw = store.read_text(config_uri)
    except Exception as e:
        raise ConfigError(f"Failed to read config: {config_uri}") from e
    try:
        result: Dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {config_uri}") from e


def _validate_request(req: Dict[str, Any]) -> None:
    if not req.get("request_id"):
        raise ConfigError("Each request must include request_id.")
    if not isinstance(req.get("source"), dict):
        raise ConfigError(
            "Each request must include source {book_type_code, asset_number}."
        )
    src = req["source"]
    if not src.get("book_type_code") or not src.get("asset_number"):
        raise ConfigError("source.book_type_code and source.asset_number are required.")
    t = str(req.get("transfer_type", "")).upper()
    if t not in ("SAME_BOOK", "XBOOK"):
        raise ConfigError("transfer_type must be SAME_BOOK or XBOOK.")
    if t == "XBOOK":
        if not isinstance(req.get("target"), dict) and not isinstance(
            req.get("xbook"), dict
        ):
            raise ConfigError(
                "XBOOK requires either target{book_type_code,...} or xbook{...} configuration."
            )


def _run_bip_flow(
    config: Dict[str, Any], store: ArtifactStore, execute: bool
) -> None:
    """BIP-driven flow: discover transfers from 'books' config via BIP report."""
    oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
    fusion_client = OracleErpIntegrationsClient(oracle_cfg)

    bip_cfg = BIPConfig.from_dict(config.get("bip", {}))
    bip_client = BIPClient(bip_cfg)

    entity_map = config.get("entity_book_map", {})
    if not entity_map:
        raise ConfigError("Config must include non-empty 'entity_book_map'.")
    entity_resolver = EntityBookResolver(entity_map)

    books = config.get("books", [])

    dff_config = None
    if config.get("dff_columns"):
        dff_config = DFFConfig(**config["dff_columns"])

    bip_params = config.get("bip_params")
    max_transfers = int(config.get("max_transfers", 500))
    dry_run = not execute

    routing_resolver = None
    routing_block = config.get("routing_rules_config") or {}
    if routing_block.get("rules") or routing_block.get("blocked_locations"):
        from .routing_rules import RoutingRulesResolver

        routing_resolver = RoutingRulesResolver.from_config(routing_block)
        log.info(
            "Routing rules loaded: %d rule(s), %d blocked location(s)",
            len(routing_resolver.rules),
            len(routing_resolver.blocked_locations),
        )

    sync = FusionIUSync(
        fusion_client,
        entity_resolver,
        bip_client,
        dff_config=dff_config,
        blocked_books=config.get("blocked_books") or [],
        routing_resolver=routing_resolver,
    )
    summary = sync.run_full_sync(
        books=books,
        dry_run=dry_run,
        max_transfers=max_transfers,
        bip_params=bip_params,
    )

    store.write_json("summary.json", summary)
    store.write_json("results.json", summary.get("results", []))

    # Publish Tableau-friendly NDJSON (always local, optionally GCS)
    results_list = summary.get("results", [])

    local_pub = LocalResultPublisher(store.base_uri)
    local_pub.publish(summary, results_list)

    gcs_block = config.get("gcs")
    if gcs_block:
        try:
            gcs_cfg = GCSPublisherConfig.from_dict(gcs_block)
            gcs_pub = GCSResultPublisher(gcs_cfg)
            gcs_pub.publish(summary, results_list)
        except Exception:
            log.exception("Failed to publish to GCS (non-fatal)")

    log.info("Run completed: %s", summary.get("counts", {}))


def run_job(
    config_uri: str, out_dir_uri: str, cli_execute: Optional[bool] = None
) -> None:
    config = _load_config(config_uri)
    store = ArtifactStore(base_uri=out_dir_uri)
    store.ensure_dir()

    execute_cfg = bool(config.get("execute", False))
    execute = execute_cfg if cli_execute is None else bool(cli_execute)

    # Route to BIP-driven flow when config has 'books' instead of 'requests'.
    if config.get("books") and not config.get("requests"):
        store.ensure_dir("audit")
        _run_bip_flow(config, store, execute)
        return

    store.ensure_dir("audit")
    store.ensure_dir("exceptions")

    oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
    client = OracleErpIntegrationsClient(oracle_cfg)

    requests = config.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ConfigError(
            "Config must include non-empty 'requests' or 'books' list."
        )

    results: List[Dict[str, Any]] = []
    summary = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "noop": 0,
        "dry_run": (not execute),
        "started_ts": int(time.time()),
    }

    for req in requests:
        summary["total"] += 1
        try:
            _validate_request(req)
            r = _process_one(client, store, req, execute)
            results.append(r)
            if r["status"] == "SUCCESS":
                summary["success"] += 1
            elif r["status"] == "NOOP":
                summary["noop"] += 1
            else:
                summary["failed"] += 1
        except Exception as e:
            log.exception("Unhandled exception processing request")
            rid = (
                req.get("request_id", "UNKNOWN") if isinstance(req, dict) else "UNKNOWN"
            )
            err = {"request_id": rid, "status": "FAILED", "error": str(e)}
            results.append(err)
            summary["failed"] += 1
            store.write_json(
                f"exceptions/{rid}.json", {"request": req, "error": str(e)}
            )

    summary["finished_ts"] = int(time.time())
    store.write_json("summary.json", summary)
    store.write_json("results.json", results)
    log.info("Run completed: %s", summary)


def _process_one(
    client: OracleErpIntegrationsClient,
    store: ArtifactStore,
    req: Dict[str, Any],
    execute: bool,
) -> Dict[str, Any]:
    request_id = str(req["request_id"])
    transfer_type = str(req.get("transfer_type", "SAME_BOOK")).upper()
    effective_date = req.get("effective_date")
    src = req["source"]
    src_book = str(src["book_type_code"])
    src_asset_number = str(src["asset_number"])

    # Always pre-read source asset state.
    raw_get, pl_get, state = get_asset_information(client, src_book, src_asset_number)
    store.write_json(
        f"audit/{request_id}.getAssetInformation.request.json",
        {"P_BOOK_TYPE_CODE": src_book, "P_ASSET_NUMBER": src_asset_number},
    )
    store.write_json(f"audit/{request_id}.getAssetInformation.response.json", raw_get)

    context = {
        "request": req,
        "source": {
            "book_type_code": src_book,
            "asset_number": src_asset_number,
            "asset_id": state.asset_id,
        },
        "asset": {
            "asset_id": state.asset_id,
            "asset_number": state.asset_number,
            "book_type_code": state.book_type_code,
            "category_id": state.category_id,
            "date_placed_in_service": state.date_placed_in_service,
            "cost": state.cost,
            "description": state.description,
            "tag_number": state.tag_number,
        },
    }

    if transfer_type == "SAME_BOOK":
        overrides = req.get("target_assignment") or {}
        target_company = overrides.get("target_company")

        if target_company:
            # Option B: derive expense CCID by swapping the Company segment.
            company_segment_key = str(overrides.get("company_segment_key", "Segment1"))
            log.info(
                "[%s] Option B: target_company=%s, company_segment_key=%s",
                request_id,
                target_company,
                company_segment_key,
            )
            params, is_noop = build_same_book_transfer_params_option_b(
                client=client,
                state=state,
                effective_date=effective_date,
                target_company=str(target_company),
                request_id=request_id,
                company_segment_key=company_segment_key,
            )
        else:
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )

        if is_noop:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "NOOP",
                "message": "Target assignment matches current assignment; no transfer posted.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
            }

        store.write_json(
            f"audit/{request_id}.transferAsset.request.json",
            {
                "OperationName": "processTransaction-transferAsset",
                "ParameterList_params": params,
            },
        )
        if not execute:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "SUCCESS",
                "dry_run": True,
                "message": "Dry-run: transferAsset payload built; no POST executed.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
                "planned_handle": "transferAsset",
            }

        raw_txn, pl_txn = client.process_transaction("transferAsset", params)
        store.write_json(f"audit/{request_id}.transferAsset.response.json", raw_txn)

        return _result_from_fusion_response(
            request_id, transfer_type, state, "transferAsset", pl_txn
        )

    # XBOOK
    xbook = req.get("xbook") if isinstance(req.get("xbook"), dict) else None
    strategy = xbook.get("strategy", "").lower() if xbook else ""

    # Strategy 1: built-in bookTransfer builder (produces all Oracle-expected params)
    if strategy in ("native", "builtin"):
        target = req.get("target")
        if not isinstance(target, dict) or not target.get("book_type_code"):
            raise ConfigError("xbook native/builtin requires target.book_type_code.")
        dest_book = str(target["book_type_code"])
        overrides = req.get("target_assignment") or {}
        # Merge xbook-level overrides (flags, book_transfer_type_code, etc.)
        if xbook:
            for xk, xv in xbook.items():
                if xk not in ("strategy", "handle", "parameters_template"):
                    overrides.setdefault(xk, xv)

        handle = (
            str(xbook.get("handle") or "transferAsset").strip()
            if xbook
            else "transferAsset"
        )

        # If a parameters_template is provided, use template rendering (legacy native path).
        tmpl = xbook.get("parameters_template") if xbook else None
        if isinstance(tmpl, dict) and tmpl:
            rendered_params = render(tmpl, context)
            params = rendered_params
        else:
            params = build_book_transfer_params(
                state=state,
                dest_book_type_code=dest_book,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )

        store.write_json(
            f"audit/{request_id}.xbook.native.request.json",
            {"handle": handle, "params": params},
        )
        if not execute:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "SUCCESS",
                "dry_run": True,
                "message": "Dry-run: native xBook payload built; no POST executed.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
                "planned_handle": handle,
            }

        raw_txn, pl_txn = client.process_transaction(handle, params)
        store.write_json(f"audit/{request_id}.xbook.native.response.json", raw_txn)
        return _result_from_fusion_response(
            request_id, transfer_type, state, handle, pl_txn
        )

    # Strategy 2: orchestration fallback (addAsset in target book + retireAsset in source book)
    target = req.get("target")
    if not isinstance(target, dict) or not target.get("book_type_code"):
        raise ConfigError("XBOOK orchestration requires target.book_type_code.")
    tgt_book = str(target["book_type_code"])

    tgt_asset_number = str(target.get("asset_number") or "").strip()
    if not tgt_asset_number:
        # Deterministic generator (can be replaced by upstream service).
        tgt_asset_number = f"XFER_{state.asset_number}_{int(time.time())}"

    overrides = req.get("target_assignment") or {}

    add_params = build_add_asset_params(
        state=state,
        target_book_type_code=tgt_book,
        target_asset_number=tgt_asset_number,
        effective_date=effective_date,
        overrides=overrides,
        request_id=request_id,
    )
    store.write_json(
        f"audit/{request_id}.addAsset.request.json",
        {
            "OperationName": "processTransaction-addAsset",
            "ParameterList_params": add_params,
        },
    )

    retire_params = build_retire_asset_params(
        state=state, effective_date=effective_date, request_id=request_id
    )
    store.write_json(
        f"audit/{request_id}.retireAsset.request.json",
        {
            "OperationName": "processTransaction-retireAsset",
            "ParameterList_params": retire_params,
        },
    )

    if not execute:
        return {
            "request_id": request_id,
            "transfer_type": transfer_type,
            "status": "SUCCESS",
            "dry_run": True,
            "message": "Dry-run: xBook orchestration payloads built (addAsset + retireAsset); no POST executed.",
            "source_asset_id": state.asset_id,
            "source_asset_number": state.asset_number,
            "source_book_type_code": state.book_type_code,
            "target_asset_number": tgt_asset_number,
            "target_book_type_code": tgt_book,
            "planned_handles": ["addAsset", "retireAsset"],
        }

    raw_add, pl_add = client.process_transaction("addAsset", add_params)
    store.write_json(f"audit/{request_id}.addAsset.response.json", raw_add)

    # If addAsset fails, do not retire source.
    add_status = str(pl_add.get("X_RETURN_STATUS") or "").strip()
    if add_status and add_status != "S":
        raise FusionApiError(
            f"addAsset failed; not retiring source. Response: {pl_add}"
        )

    raw_ret, pl_ret = client.process_transaction("retireAsset", retire_params)
    store.write_json(f"audit/{request_id}.retireAsset.response.json", raw_ret)

    # Build consolidated result
    return {
        "request_id": request_id,
        "transfer_type": transfer_type,
        "status": "SUCCESS"
        if str(pl_ret.get("X_RETURN_STATUS") or "") == "S"
        else "FAILED",
        "mode": "orchestrated_add_retire",
        "source_asset_id": state.asset_id,
        "source_asset_number": state.asset_number,
        "source_book_type_code": state.book_type_code,
        "target_asset_number": pl_add.get("PX_ASSET_NUMBER") or tgt_asset_number,
        "target_book_type_code": tgt_book,
        "addAsset": _compact_pl(pl_add),
        "retireAsset": _compact_pl(pl_ret),
    }


def _compact_pl(pl: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "X_RETURN_STATUS",
        "X_EVENT_ID",
        "X_TRANSACTION_HEADER_ID",
        "X_D_TRANSACTION_HEADER_ID",
        "X_RETIREMENT_ID",
        "X_MSG_COUNT",
        "X_MSG_DATA",
        "PX_ASSET_NUMBER",
    ]
    return {k: pl.get(k) for k in keys if k in pl}


def _result_from_fusion_response(
    request_id: str,
    transfer_type: str,
    state: Any,
    handle: str,
    pl: Dict[str, Any],
) -> Dict[str, Any]:
    status = str(pl.get("X_RETURN_STATUS") or "").strip()
    out = {
        "request_id": request_id,
        "transfer_type": transfer_type,
        "handle": handle,
        "asset_id": getattr(state, "asset_id", None),
        "asset_number": getattr(state, "asset_number", None),
        "book_type_code": getattr(state, "book_type_code", None),
        "fusion": _compact_pl(pl),
    }
    if status == "S":
        out["status"] = "SUCCESS"
        return out

    # Per Oracle guidance, X_RETURN_STATUS can be F even when X_EVENT_ID exists;
    # treat as failure but preserve identifiers for investigation.
    out["status"] = "FAILED"
    out["message"] = (
        "Fusion returned non-success X_RETURN_STATUS; see fusion payload for details."
    )
    return out
# <<< END: job.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
