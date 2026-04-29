"""Tests for Slack / PagerDuty / GCS publishers in ofam_mass_additions."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.exceptions import ConfigError
from ofam_mass_additions.models import (
    AuditEvent,
    ExceptionRecord,
    ProposedOracleUpdate,
)
from ofam_mass_additions.runner.live_run import LiveRunResult


def _result(
    *,
    total: int = 3,
    auto_update: int = 1,
    exception_count: int = 2,
    dry_run: bool = True,
    out_dir: Path | None = None,
) -> LiveRunResult:
    proposed = [
        ProposedOracleUpdate(mass_addition_id=f"MA-A{i}", params={})
        for i in range(auto_update)
    ]
    exceptions = [
        ExceptionRecord(
            mass_addition_id=f"MA-X{i}",
            reason="CMDB not found" if i % 2 == 0 else "Inactive CCID",
            detail="validation failed",
        )
        for i in range(exception_count)
    ]
    audit_events = [
        AuditEvent(mass_addition_id=f"MA-A{i}", stage="rules", status="success", message="ok")
        for i in range(auto_update)
    ] + [
        AuditEvent(mass_addition_id=e.mass_addition_id, stage="rules", status="exception", message=e.reason)
        for e in exceptions
    ]

    return LiveRunResult(
        counts={"total": total, "auto_update": auto_update, "exception": exception_count},
        proposed=proposed,
        exceptions=exceptions,
        audit_events=audit_events,
        run_date="2026-04-29",
        run_ts="2026-04-29T18:00:00Z",
        dry_run=dry_run,
        pilot_book="US CORP BOOK",
        pilot_region="US",
        out_dir=out_dir or Path("/tmp/ma-test"),
    )


# ============================================================================
# Slack
# ============================================================================
def test_slack_from_dict_requires_webhook() -> None:
    from ofam_mass_additions.publishers.slack_publisher import SlackConfig

    with pytest.raises(ValueError, match="webhook_url"):
        SlackConfig.from_dict({})


def test_slack_from_dict_reads_env(monkeypatch) -> None:
    from ofam_mass_additions.publishers.slack_publisher import SlackConfig

    monkeypatch.setenv("MY_SLACK", "https://hooks.slack.com/services/X/Y/Z")
    cfg = SlackConfig.from_dict({"webhook_url_env": "MY_SLACK", "max_failures": 3})
    assert cfg.webhook_url == "https://hooks.slack.com/services/X/Y/Z"
    assert cfg.max_failures == 3


def test_slack_publish_clean_run_emits_check_emoji() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        SlackConfig,
        SlackPublisher,
    )

    pub = SlackPublisher(
        SlackConfig(webhook_url="https://example.com/hook", dashboard_url="https://x")
    )
    posted = MagicMock(status_code=200, text="ok")
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        return_value=posted,
    ) as p:
        pub.publish(_result(total=3, auto_update=3, exception_count=0))

    body = p.call_args.kwargs["json"]["text"]
    assert ":white_check_mark:" in body
    assert "FA Mass Additions Complete" in body
    assert "Total: 3 | Auto-update: 3 | Exceptions: 0" in body
    assert "Open Dashboard" in body


def test_slack_publish_with_exceptions_uses_warning_and_truncates() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        SlackConfig,
        SlackPublisher,
    )

    pub = SlackPublisher(
        SlackConfig(webhook_url="https://example.com/hook", max_failures=2)
    )
    posted = MagicMock(status_code=200, text="ok")
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        return_value=posted,
    ) as p:
        pub.publish(_result(total=10, auto_update=0, exception_count=5))

    body = p.call_args.kwargs["json"]["text"]
    assert ":warning:" in body
    assert "By reason:" in body
    # Only max_failures exceptions are listed, with an "...and N more" footer.
    assert "and 3 more" in body


def test_slack_swallows_post_errors() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        SlackConfig,
        SlackPublisher,
    )

    pub = SlackPublisher(SlackConfig(webhook_url="https://example.com/hook"))
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        side_effect=RuntimeError("boom"),
    ):
        pub.publish(_result())  # must not raise


# ============================================================================
# PagerDuty
# ============================================================================
def test_pagerduty_from_dict_requires_routing_key() -> None:
    from ofam_mass_additions.publishers.slack_publisher import PagerDutyConfig

    with pytest.raises(ValueError, match="routing_key"):
        PagerDutyConfig.from_dict({})


def test_pagerduty_check_and_trigger_silent_when_total_zero() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        PagerDutyConfig,
        PagerDutyPublisher,
    )

    pub = PagerDutyPublisher(PagerDutyConfig(routing_key="rk"))
    with patch("ofam_mass_additions.publishers.slack_publisher.http.post") as p:
        pub.check_and_trigger(_result(total=0, auto_update=0, exception_count=0))
    p.assert_not_called()


def test_pagerduty_triggers_when_all_rows_hit_exceptions() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        PagerDutyConfig,
        PagerDutyPublisher,
    )

    pub = PagerDutyPublisher(PagerDutyConfig(routing_key="rk"))
    posted = MagicMock(status_code=202, text="accepted")
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        return_value=posted,
    ) as p:
        pub.check_and_trigger(_result(total=4, auto_update=0, exception_count=4))

    body = p.call_args.kwargs["json"]["payload"]
    assert "ALL 4 rows hit exceptions" in body["summary"]
    assert body["severity"] == "critical"


def test_pagerduty_triggers_at_or_above_threshold() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        PagerDutyConfig,
        PagerDutyPublisher,
    )

    pub = PagerDutyPublisher(PagerDutyConfig(routing_key="rk", failure_threshold_pct=50))
    posted = MagicMock(status_code=202, text="accepted")
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        return_value=posted,
    ) as p:
        # 4 exceptions out of 5 = 80% — above 50% threshold
        pub.check_and_trigger(_result(total=5, auto_update=1, exception_count=4))

    summary = p.call_args.kwargs["json"]["payload"]["summary"]
    assert "exception rate" in summary


def test_pagerduty_does_not_trigger_below_threshold() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        PagerDutyConfig,
        PagerDutyPublisher,
    )

    pub = PagerDutyPublisher(PagerDutyConfig(routing_key="rk", failure_threshold_pct=80))
    with patch("ofam_mass_additions.publishers.slack_publisher.http.post") as p:
        # 1 of 10 = 10% — well below 80%
        pub.check_and_trigger(_result(total=10, auto_update=9, exception_count=1))
    p.assert_not_called()


def test_pagerduty_trigger_if_hard_error_includes_class() -> None:
    from ofam_mass_additions.publishers.slack_publisher import (
        PagerDutyConfig,
        PagerDutyPublisher,
    )

    pub = PagerDutyPublisher(PagerDutyConfig(routing_key="rk"))
    posted = MagicMock(status_code=202, text="accepted")
    with patch(
        "ofam_mass_additions.publishers.slack_publisher.http.post",
        return_value=posted,
    ) as p:
        pub.trigger_if_hard_error(RuntimeError("network down"))

    payload = p.call_args.kwargs["json"]["payload"]
    assert "RuntimeError" in payload["summary"]
    assert "network down" in payload["custom_details"]["error"]


# ============================================================================
# GCS
# ============================================================================
def test_gcs_from_dict_requires_bucket() -> None:
    from ofam_mass_additions.publishers.gcs_publisher import GCSPublisherConfig

    with pytest.raises(ConfigError, match="bucket"):
        GCSPublisherConfig.from_dict({})


def _install_fake_gcs_module() -> tuple[MagicMock, MagicMock]:
    """Inject a fake google.cloud.storage with a Client we can introspect."""
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client.bucket.return_value = fake_bucket

    storage_mod = types.SimpleNamespace(
        Client=MagicMock(return_value=fake_client)
    )
    storage_mod.Client.from_service_account_json = MagicMock(return_value=fake_client)

    cloud_mod = types.SimpleNamespace(storage=storage_mod)
    google_mod = types.SimpleNamespace(cloud=cloud_mod)

    sys.modules["google"] = google_mod
    sys.modules["google.cloud"] = cloud_mod
    sys.modules["google.cloud.storage"] = storage_mod

    return fake_bucket, fake_blob


def test_gcs_publish_uploads_files_and_manifest(tmp_path: Path) -> None:
    from ofam_mass_additions.publishers.gcs_publisher import (
        GCSAuditPublisher,
        GCSPublisherConfig,
    )

    # Two of three artefacts exist; missing exceptions.csv should be skipped.
    (tmp_path / "proposed_updates.csv").write_text("hdr\n")
    (tmp_path / "audit.jsonl").write_text('{"x":1}\n')

    fake_bucket, fake_blob = _install_fake_gcs_module()

    cfg = GCSPublisherConfig(bucket="my-bucket", prefix="mass_adds/audit")
    pub = GCSAuditPublisher(cfg)

    written = pub.publish(_result(out_dir=tmp_path))

    # 2 files uploaded + 1 manifest = 3 calls to .blob()
    blob_paths = [c.args[0] for c in fake_bucket.blob.call_args_list]
    assert any("proposed_updates.csv" in p for p in blob_paths)
    assert any("audit.jsonl" in p for p in blob_paths)
    assert any("manifest.json" in p for p in blob_paths)
    # exceptions.csv was missing, so it must not appear:
    assert not any("exceptions.csv" in p for p in blob_paths)

    # Returned URIs match what was uploaded.
    assert any("gs://my-bucket/mass_adds/audit/2026-04-29/" in u for u in written)
    assert any(u.endswith("manifest.json") for u in written)


def test_gcs_run_folder_includes_run_ts_for_uniqueness(tmp_path: Path) -> None:
    """Two runs on the same date should land in distinct folders."""
    from ofam_mass_additions.publishers.gcs_publisher import (
        GCSAuditPublisher,
        GCSPublisherConfig,
    )

    cfg = GCSPublisherConfig(bucket="b", prefix="p")
    pub = GCSAuditPublisher(cfg)

    r1 = LiveRunResult(
        counts={}, run_date="2026-04-29", run_ts="2026-04-29T10:00:00Z",
        out_dir=tmp_path,
    )
    r2 = LiveRunResult(
        counts={}, run_date="2026-04-29", run_ts="2026-04-29T11:30:00Z",
        out_dir=tmp_path,
    )

    assert pub._run_folder(r1) != pub._run_folder(r2)
    assert pub._run_folder(r1).startswith("p/2026-04-29/")
