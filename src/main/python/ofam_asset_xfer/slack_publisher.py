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

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List

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
        """Build a SlackConfig from a raw config dictionary."""
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
        """Initialise the Slack publisher with config."""
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SlackPublisher":
        """Create a SlackPublisher from a raw config dict."""
        return SlackPublisher(SlackConfig.from_dict(d))

    def publish(
        self, summary: Dict[str, Any], results: List[Dict[str, Any]]
    ) -> None:
        """Format and send Slack notification."""
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
    failure_threshold_pct: float = 80.0  # trigger if failure rate >= this %

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PagerDutyConfig":
        """Build a PagerDutyConfig from a raw config dictionary."""
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
            failure_threshold_pct=float(d.get("failure_threshold_pct", 80.0)),
        )


class PagerDutyPublisher:
    """Triggers PagerDuty incidents for hard errors and high failure rates.

    Triggers on:
      - Hard crash (exception in top-level handler)
      - High failure rate (failure_threshold_pct exceeded, default 80%)
      - Zero transfers with failures (e.g. expired JWT → all 401s)
    """

    def __init__(self, cfg: PagerDutyConfig):
        """Initialise the PagerDuty publisher with config."""
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PagerDutyPublisher":
        """Create a PagerDutyPublisher from a raw config dict."""
        return PagerDutyPublisher(PagerDutyConfig.from_dict(d))

    def trigger_if_hard_error(self, error: Exception) -> None:
        """Trigger for a hard crash (top-level exception handler)."""
        summary = f"FA Asset Transfer job crashed: {type(error).__name__}: {str(error)[:200]}"
        self._trigger(summary, str(error))

    def check_and_trigger(
        self, summary: Dict[str, Any], results: List[Dict[str, Any]]
    ) -> None:
        """Check job results and trigger if failure rate is too high.

        Triggers when:
          - Total > 0 and transferred == 0 (likely auth/infra issue)
          - Failure rate >= failure_threshold_pct (default 80%)
        """
        counts = summary.get("counts", {})
        total = counts.get("total", 0)
        transferred = counts.get("transferred", 0)
        failed = counts.get("failed", 0)

        if total == 0:
            return  # nothing to check

        failure_pct = (failed / total) * 100 if total > 0 else 0
        threshold = self.cfg.failure_threshold_pct

        if transferred == 0 and failed > 0:
            # All failed, zero success → likely auth/infra problem
            detail = f"0/{total} transferred, {failed} failed. Likely auth or infrastructure issue."
            sample_errors = [
                str(r.get("error", ""))[:150]
                for r in results
                if r.get("status") == "FAILED"
            ][:3]
            if sample_errors:
                detail += " Sample errors: " + " | ".join(sample_errors)
            self._trigger(
                f"FA Asset Transfer: ALL {total} assets failed (0 transferred)",
                detail,
            )
        elif failure_pct >= threshold:
            self._trigger(
                f"FA Asset Transfer: {failure_pct:.0f}% failure rate "
                f"({failed}/{total} failed, {transferred} transferred)",
                f"Failure rate {failure_pct:.0f}% exceeds threshold {threshold}%.",
            )

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
