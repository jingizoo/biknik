"""Publish transfer summary to Slack via incoming webhook.

Sends a single message with:
  1. Overall counts (total, transferred, failed, dry-run)
  2. Per-book breakdown (source → target: ok / failed)
  3. Failed asset details (truncated after ``max_failures``)

Configuration
-------------
::

    {
      "slack": {
        "webhook_url_env": "SLACK_WEBHOOK_URL",
        "dashboard_url": "https://tableau.example.com/...",
        "max_failures": 10
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

log = logging.getLogger(__name__)


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
        self.cfg = cfg

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
        title = "FA Asset Transfer Complete" if is_success else "FA Asset Transfer — Failures Detected"

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
                lines.append(f"  • {src} → {tgt}: {status}")

        # Failed assets
        failures = [r for r in results if r.get("status") == "FAILED"]
        if failures:
            lines.append("")
            show = failures[: self.cfg.max_failures]
            lines.append(f"*Failed Assets ({len(failures)}):*")
            for r in show:
                asset = r.get("asset_number", "?")
                error = r.get("error", "unknown error")
                # Truncate long error messages
                if len(str(error)) > 120:
                    error = str(error)[:117] + "..."
                lines.append(f"  • {asset}: {error}")
            if len(failures) > self.cfg.max_failures:
                lines.append(
                    f"  _...and {len(failures) - self.cfg.max_failures} more — see dashboard_"
                )

        # Dashboard link
        if self.cfg.dashboard_url:
            lines.append("")
            lines.append(f"<{self.cfg.dashboard_url}|:bar_chart: Open Dashboard>")

        text = "\n".join(lines)
        self._post(text)
        log.info("Slack notification sent (%d chars)", len(text))

    def _post(self, text: str) -> None:
        """POST message to Slack webhook."""
        payload = {"text": text}
        resp = http.post(
            self.cfg.webhook_url,
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(
                "Slack webhook returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )


def _build_book_breakdown(
    results: List[Dict[str, Any]],
) -> Dict[tuple, Dict[str, int]]:
    """Aggregate results by (source_book, target_book)."""
    stats: Dict[tuple, Dict[str, int]] = defaultdict(lambda: {"ok": 0, "fail": 0})
    for r in results:
        src = r.get("source_book", "?")
        tgt = r.get("target_book", "?")
        key = (src, tgt)
        if r.get("status") == "FAILED":
            stats[key]["fail"] += 1
        else:
            stats[key]["ok"] += 1
    return dict(stats)
