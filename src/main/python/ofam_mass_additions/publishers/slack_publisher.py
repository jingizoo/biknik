"""Slack + PagerDuty publishers for mass-additions runs.

Both go through the corporate proxy when configured (reads
HTTP_APP_PROXY / HTTPS_APP_PROXY / INETPROXY_USER / INETPROXY_PASSWD
from environment via :mod:`ofam_mass_additions.proxy_config`).

Configuration::

    "slack": {
      "webhook_url_env": "SLACK_WEBHOOK_URL",
      "dashboard_url":   "https://tableau.example.com/...",
      "max_failures":    10
    }
    "pagerduty": {
      "routing_key_env":       "PAGERDUTY_ROUTING_KEY",
      "severity":              "critical",
      "source":                "fa-mass-additions",
      "failure_threshold_pct": 80
    }

The Slack message and PagerDuty thresholds reflect the mass-additions
shape (auto-update / exception counts) — not the asset-xfer
transferred / failed counts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List

import requests as http  # type: ignore[import-untyped]

from ofam_mass_additions.models import ExceptionRecord
from ofam_mass_additions.proxy_config import get_proxy_config
from ofam_mass_additions.runner.live_run import LiveRunResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SlackConfig:
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
    def __init__(self, cfg: SlackConfig) -> None:
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SlackPublisher":
        return SlackPublisher(SlackConfig.from_dict(d))

    def publish(self, result: LiveRunResult) -> None:
        counts = result.counts
        total = counts.get("total", 0)
        auto_update = counts.get("auto_update", 0)
        exception_count = counts.get("exception", 0)

        is_clean = exception_count == 0
        emoji = ":white_check_mark:" if is_clean else ":warning:"
        mode = "DRY-RUN" if result.dry_run else "LIVE"
        title = (
            f"FA Mass Additions Complete ({mode})"
            if is_clean
            else f"FA Mass Additions — Exceptions Detected ({mode})"
        )

        lines: List[str] = [
            f"{emoji} *{title}*",
            (
                f"Book: {result.pilot_book} | Region: {result.pilot_region}"
                f" | Total: {total} | Auto-update: {auto_update}"
                f" | Exceptions: {exception_count}"
            ),
        ]
        if result.run_date or result.run_ts:
            lines.append(f"Date: {result.run_date} | Run: {result.run_ts}")

        if result.exceptions:
            lines.append("")
            lines.append(_exceptions_breakdown(result.exceptions))
            lines.append("")
            show = result.exceptions[: self.cfg.max_failures]
            lines.append(f"*Top exceptions ({len(show)}/{len(result.exceptions)}):*")
            for exc in show:
                detail = exc.detail or ""
                if len(detail) > 120:
                    detail = detail[:117] + "..."
                lines.append(f"  • `{exc.mass_addition_id}` — {exc.reason}: {detail}")
            if len(result.exceptions) > self.cfg.max_failures:
                lines.append(
                    f"  _...and {len(result.exceptions) - self.cfg.max_failures}"
                    " more — see exceptions.csv / dashboard_"
                )

        if self.cfg.dashboard_url:
            lines.append("")
            lines.append(f"<{self.cfg.dashboard_url}|:bar_chart: Open Dashboard>")

        self._post("\n".join(lines))

    def _post(self, text: str) -> None:
        try:
            resp = http.post(
                self.cfg.webhook_url,
                json={"text": text},
                timeout=15,
                proxies=self._proxies,
            )
            if resp.status_code != 200:
                log.warning(
                    "Slack webhook returned %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                log.info("Slack notification sent (%d chars)", len(text))
        except Exception:
            log.exception("Failed to post Slack notification (non-fatal)")


def _exceptions_breakdown(exceptions: List[ExceptionRecord]) -> str:
    """Group exceptions by reason for a one-liner summary."""
    counts: Dict[str, int] = {}
    for exc in exceptions:
        counts[exc.reason] = counts.get(exc.reason, 0) + 1
    if not counts:
        return ""
    parts = [f"{n} {reason}" for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "*By reason:* " + " | ".join(parts)


# ---------------------------------------------------------------------------
# PagerDuty
# ---------------------------------------------------------------------------
PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"


@dataclass(frozen=True)
class PagerDutyConfig:
    routing_key: str
    severity: str = "critical"
    source: str = "fa-mass-additions"
    failure_threshold_pct: float = 80.0

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
            source=str(d.get("source", "fa-mass-additions")).strip(),
            failure_threshold_pct=float(d.get("failure_threshold_pct", 80.0)),
        )


class PagerDutyPublisher:
    def __init__(self, cfg: PagerDutyConfig) -> None:
        self.cfg = cfg
        self._proxies = get_proxy_config()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PagerDutyPublisher":
        return PagerDutyPublisher(PagerDutyConfig.from_dict(d))

    def trigger_if_hard_error(self, error: Exception) -> None:
        summary = (
            f"FA Mass Additions job crashed: "
            f"{type(error).__name__}: {str(error)[:200]}"
        )
        self._trigger(summary, str(error))

    def check_and_trigger(self, result: LiveRunResult) -> None:
        """Trigger when nothing auto-updated despite work, or exception% high."""
        counts = result.counts
        total = counts.get("total", 0)
        auto_update = counts.get("auto_update", 0)
        exception_count = counts.get("exception", 0)
        if total == 0:
            return  # nothing happened — silent

        if auto_update == 0 and exception_count > 0:
            sample = [
                f"{e.mass_addition_id}: {e.reason}"
                for e in result.exceptions[:3]
            ]
            detail = (
                f"0/{total} auto-updated, {exception_count} exceptions. "
                "Likely auth or schema-mapping issue. Sample: "
                + " | ".join(sample)
            )
            self._trigger(
                f"FA Mass Additions: ALL {total} rows hit exceptions (0 auto-updated)",
                detail,
            )
            return

        failure_pct = (exception_count / total) * 100 if total else 0
        if failure_pct >= self.cfg.failure_threshold_pct:
            self._trigger(
                f"FA Mass Additions: {failure_pct:.0f}% exception rate "
                f"({exception_count}/{total} exceptions, {auto_update} auto-updated)",
                f"Exception rate {failure_pct:.0f}% >= threshold "
                f"{self.cfg.failure_threshold_pct}%.",
            )

    def _trigger(self, summary: str, details: str) -> None:
        payload = {
            "routing_key": self.cfg.routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": summary[:1024],
                "severity": self.cfg.severity,
                "source": self.cfg.source,
                "component": "mass-additions-pipeline",
                "group": "oracle-fusion",
                "class": "batch-job-failure",
                "custom_details": {"error": details[:2000]},
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
