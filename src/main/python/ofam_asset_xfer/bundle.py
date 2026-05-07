# =============================================================================
# MERGED FILE — 2 original modules joined as-is with section markers.
#
#   Section 1 → batch_entrypoint.py
#   Section 2 → slack_publisher.py
#
# To find section boundaries:
#   grep -n "^# >>> FILE\|^# <<< END" bundle.py
# =============================================================================

# >>> FILE: batch_entrypoint.py >>>
#!/usr/bin/env python3
"""CDX Batch entrypoint for OFAM IU Asset Transfer (BIP-driven discovery).

Discovers all pending Inter-Unit transfers from a BI Publisher report and
executes them.  No individual asset IDs or numbers are required — the BIP
report (called with just the book code) returns all eligible IU assets.

Example:
  python batch_entrypoint.py --config /tmp/config.json --out-dir /tmp/out --dry-run
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import typer

from ofam_asset_xfer.bip_client import BIPClient, BIPConfig
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.fusion_sync import FusionIUSync, DFFConfig
from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from ofam_asset_xfer.local_publisher import LocalResultPublisher
from ofam_asset_xfer.slack_publisher import SlackPublisher, PagerDutyPublisher
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.store import ArtifactStore

app = typer.Typer(help="OFAM IU Asset Transfer Automation (BIP-driven).")


@app.command()
def main(
    config: str = typer.Option(..., help="Path/URI to config JSON."),
    out_dir: str = typer.Option(..., help="Output directory/URI for artifacts."),
    dry_run: bool = typer.Option(False, help="Reads/validation only (no writes to Fusion)."),
    execute: bool = typer.Option(False, help="Post transactions to Fusion. Overrides --dry-run."),
    log_level: str = typer.Option(
        os.getenv("LOG_LEVEL", "INFO"), help="Logging level (INFO, DEBUG, ...)."
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help=(
            "Enable DEBUG logging and dump every Fusion request/response "
            "(auth-redacted) to <out-dir>/debug-dumps/ for offline inspection."
        ),
    ),
    debug_dir: str = typer.Option(
        None,
        "--debug-dir",
        help="Override directory for --debug dumps (default: <out-dir>/debug-dumps/).",
    ),
) -> None:
    effective_log_level = "DEBUG" if debug else log_level
    logging.basicConfig(
        level=getattr(logging, str(effective_log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log = logging.getLogger("batch_entrypoint")

    # Execution flag: --execute wins, else dry-run (default).
    should_execute = execute and not dry_run

    resolved_debug_dir: str | None = None
    if debug:
        from pathlib import Path as _P
        resolved_debug_dir = debug_dir or str(_P(out_dir) / "debug-dumps")

    try:
        store = ArtifactStore(base_uri=".")
        raw = store.read_text(config)
        cfg = json.loads(raw)

        # --- Build clients from config ---
        oracle_cfg = OracleConfig.from_dict(cfg.get("oracle", {}))
        fusion_client = OracleErpIntegrationsClient(oracle_cfg, debug_dir=resolved_debug_dir)

        bip_cfg = BIPConfig.from_dict(cfg.get("bip", {}))
        bip_client = BIPClient(bip_cfg)

        entity_map = cfg.get("entity_book_map", {})
        if not entity_map:
            raise ConfigError("Config must include non-empty 'entity_book_map'.")
        entity_resolver = EntityBookResolver(entity_map)

        books = cfg.get("books", [])
        if not books:
            raise ConfigError("Config must include non-empty 'books' list.")

        dff_config = None
        if cfg.get("dff_columns"):
            dff_config = DFFConfig(**cfg["dff_columns"])

        bip_params = cfg.get("bip_params")
        max_transfers = int(cfg.get("max_transfers", 500))

        # --- Run ---
        sync = FusionIUSync(
            fusion_client,
            entity_resolver,
            bip_client,
            dff_config=dff_config,
            default_transfer_date=cfg.get("default_transfer_date"),
            blocked_books=cfg.get("blocked_books") or [],
            transfer_overrides=cfg.get("transfer_overrides") or {},
        )
        summary = sync.run_full_sync(
            books=books,
            dry_run=(not should_execute),
            max_transfers=max_transfers,
            bip_params=bip_params,
        )

        # Write raw results (JSON)
        out_store = ArtifactStore(base_uri=out_dir)
        out_store.ensure_dir()
        out_store.write_json("summary.json", summary)
        out_store.write_json("results.json", summary.get("results", []))

        # Publish Tableau-friendly NDJSON (always local, optionally GCS)
        results_list = summary.get("results", [])

        local_pub = LocalResultPublisher(out_dir)
        local_pub.publish(summary, results_list)

        gcs_block = cfg.get("gcs")
        if gcs_block:
            try:
                gcs_cfg = GCSPublisherConfig.from_dict(gcs_block)
                gcs_pub = GCSResultPublisher(gcs_cfg)
                gcs_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to publish to GCS (non-fatal)")

        slack_block = cfg.get("slack")
        if slack_block:
            try:
                slack_pub = SlackPublisher.from_dict(slack_block)
                slack_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to send Slack notification (non-fatal)")

        counts = summary.get("counts", {})
        log.info("Completed: total=%s transferred=%s failed=%s dry_run=%s",
                 counts.get("total", 0), counts.get("transferred", 0),
                 counts.get("failed", 0), counts.get("dry_run", 0))

    except Exception as exc:
        log.exception("Fatal error running job")
        pd_block = cfg.get("pagerduty") if "cfg" in dir() else None
        if pd_block:
            try:
                pd_pub = PagerDutyPublisher.from_dict(pd_block)
                pd_pub.trigger_if_hard_error(exc)
            except Exception:
                log.exception("Failed to trigger PagerDuty (non-fatal)")
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
# <<< END: batch_entrypoint.py <<<

# >>> FILE: slack_publisher.py >>>
"""Publish transfer summary to Slack and optionally PagerDuty.

Slack:  Summary + per-book breakdown + failed asset details via webhook.
PagerDuty: Triggers an incident only on **hard errors** (auth failures,
           network errors, API unreachable) — NOT on individual asset
           validation/transfer failures.

Both Slack and PagerDuty requests go through the corporate proxy when
configured (reads HTTP_APP_PROXY / HTTPS_APP_PROXY / INETPROXY_USER /
INETPROXY_PASSWD from environment).

Configuration
-------------
::

    {
      "slack": {
        "webhook_url_env": "SLACK_WEBHOOK_URL",
        "dashboard_url": "https://tableau.example.com/...",
        "max_failures": 10
      },
      "pagerduty": {
        "routing_key_env": "PAGERDUTY_ROUTING_KEY",
        "severity": "critical",
        "source": "fa-asset-xfer"
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests as http  # type: ignore[import-untyped]

from .proxy_config import get_proxy_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SlackConfig:
    """Configuration for Slack webhook publisher."""

    webhook_url: str
    dashboard_url: str = ""
    max_failures: int = 10

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SlackConfig":
        webhook_url = d.get("webhook_url", "")
        webhook_url_env = d.get("webhook_url_env")
        if webhook_url_env:
            webhook_url = os.getenv(str(webhook_url_env), webhook_url)

        if not webhook_url:
            raise ValueError(
                "slack.webhook_url is required (webhook_url or webhook_url_env)"
            )

        return SlackConfig(
            webhook_url=str(webhook_url),
            dashboard_url=str(d.get("dashboard_url", "")),
            max_failures=int(d.get("max_failures", 10)),
        )


class SlackPublisher:
    """Publishes transfer summary to a Slack channel via webhook."""

    def __init__(self, cfg: SlackConfig):
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SlackPublisher":
        return SlackPublisher(SlackConfig.from_dict(d))

    def publish(
        self, summary: Dict[str, Any], results: List[Dict[str, Any]]
    ) -> None:
        counts = summary.get("counts", {})
        total = counts.get("total", len(results))
        transferred = counts.get("transferred", 0)
        failed = counts.get("failed", 0)
        dry_run_count = counts.get("dry_run", 0)
        run_date = summary.get("run_date", "")
        run_ts = summary.get("run_ts", "")

        is_success = failed == 0
        emoji = ":white_check_mark:" if is_success else ":warning:"
        title = (
            "FA Asset Transfer Complete"
            if is_success
            else "FA Asset Transfer — Failures Detected"
        )

        lines: List[str] = []
        lines.append(f"{emoji} *{title}*")
        lines.append(
            f"Total: {total} | Transferred: {transferred} "
            f"| Failed: {failed} | Dry-run: {dry_run_count}"
        )
        if run_date or run_ts:
            lines.append(f"Date: {run_date} | Run: {run_ts}")

        # Per-book breakdown
        book_stats = _build_book_breakdown(results)
        if book_stats:
            lines.append("")
            lines.append("*Per-Book Breakdown:*")
            for key, stats in sorted(book_stats.items()):
                src, tgt = key
                ok = stats["ok"]
                fail = stats["fail"]
                status = f"{ok} ok, {fail} failed" if fail else f"{ok} ok"
                lines.append(f"  \u2022 {src} \u2192 {tgt}: {status}")

        # Failed assets
        failures = [r for r in results if r.get("status") == "FAILED"]
        if failures:
            lines.append("")
            show = failures[: self.cfg.max_failures]
            lines.append(f"*Failed Assets ({len(failures)}):*")
            for r in show:
                asset = r.get("asset_number", "?")
                error = r.get("error", "unknown error")
                if len(str(error)) > 120:
                    error = str(error)[:117] + "..."
                lines.append(f"  \u2022 {asset}: {error}")
            if len(failures) > self.cfg.max_failures:
                lines.append(
                    f"  _...and {len(failures) - self.cfg.max_failures} more \u2014 see dashboard_"
                )

        # Dashboard link
        if self.cfg.dashboard_url:
            lines.append("")
            lines.append(f"<{self.cfg.dashboard_url}|:bar_chart: Open Dashboard>")

        text = "\n".join(lines)
        self._post(text)
        log.info("Slack notification sent (%d chars)", len(text))

    def _post(self, text: str) -> None:
        payload = {"text": text}
        resp = http.post(
            self.cfg.webhook_url,
            json=payload,
            timeout=15,
            proxies=self._proxies,
        )
        if resp.status_code != 200:
            log.warning(
                "Slack webhook returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )


# ---------------------------------------------------------------------------
# PagerDuty
# ---------------------------------------------------------------------------
# PagerDuty Events API v2: https://developer.pagerduty.com/api-reference/
# Triggers an incident via the Events API.
#
# Required: a routing key (Integration Key) from a PagerDuty service.
# To get one:
#   1. Go to PagerDuty → Services → your service → Integrations
#   2. Add integration → "Events API v2"
#   3. Copy the "Integration Key" (32-char hex string)
#   4. Store as PAGERDUTY_ROUTING_KEY in Holocron Vault / K8s secret

PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"


@dataclass(frozen=True)
class PagerDutyConfig:
    """Configuration for PagerDuty Events API v2."""

    routing_key: str
    severity: str = "critical"  # critical, error, warning, info
    source: str = "fa-asset-xfer"

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PagerDutyConfig":
        routing_key = d.get("routing_key", "")
        routing_key_env = d.get("routing_key_env")
        if routing_key_env:
            routing_key = os.getenv(str(routing_key_env), routing_key)

        if not routing_key:
            raise ValueError(
                "pagerduty.routing_key is required (routing_key or routing_key_env)"
            )

        severity = str(d.get("severity", "critical")).strip().lower()
        if severity not in ("critical", "error", "warning", "info"):
            severity = "critical"

        return PagerDutyConfig(
            routing_key=str(routing_key),
            severity=severity,
            source=str(d.get("source", "fa-asset-xfer")).strip(),
        )


class PagerDutyPublisher:
    """Triggers PagerDuty incidents for hard errors only.

    Hard errors: auth failures, network errors, proxy errors, API unreachable.
    NOT triggered for: individual asset transfer failures (validation, ORA errors).
    """

    def __init__(self, cfg: PagerDutyConfig):
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PagerDutyPublisher":
        return PagerDutyPublisher(PagerDutyConfig.from_dict(d))

    def trigger_if_hard_error(self, error: Exception) -> None:
        """Trigger a PagerDuty incident for a hard/infrastructure error.

        Call this from the entrypoint's top-level exception handler —
        not for per-asset failures.
        """
        summary = f"FA Asset Transfer job failed: {type(error).__name__}: {str(error)[:200]}"
        self._trigger(summary, str(error))

    def _trigger(self, summary: str, details: str) -> None:
        payload = {
            "routing_key": self.cfg.routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": summary[:1024],
                "severity": self.cfg.severity,
                "source": self.cfg.source,
                "component": "assetxferpipeline",
                "group": "oracle-fusion",
                "class": "batch-job-failure",
                "custom_details": {
                    "error": details[:2000],
                },
            },
        }

        try:
            resp = http.post(
                PAGERDUTY_EVENTS_URL,
                json=payload,
                timeout=15,
                proxies=self._proxies,
            )
            if resp.status_code == 202:
                log.info("PagerDuty incident triggered: %s", summary[:80])
            else:
                log.warning(
                    "PagerDuty returned %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            log.exception("Failed to trigger PagerDuty incident (non-fatal)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_book_breakdown(
    results: List[Dict[str, Any]],
) -> Dict[tuple[str, str], Dict[str, int]]:
    stats: Dict[tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"ok": 0, "fail": 0}
    )
    for r in results:
        src = r.get("source_book", "?")
        tgt = r.get("target_book", "?")
        key = (src, tgt)
        if r.get("status") == "FAILED":
            stats[key]["fail"] += 1
        else:
            stats[key]["ok"] += 1
    return dict(stats)
# <<< END: slack_publisher.py <<<
