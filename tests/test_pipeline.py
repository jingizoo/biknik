"""Tests for the shared run_pipeline orchestrator."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ofam_asset_xfer import pipeline
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.pipeline import _load_pre_bip_block, run_pipeline


@pytest.fixture
def log() -> logging.Logger:
    return logging.getLogger("test_pipeline")


def _base_config() -> dict:
    return {
        "oracle": {"base_url": "http://x", "bearer_token_env": "FUSION_JWT"},
        "bip": {"base_url": "http://x", "bearer_token_env": "FUSION_JWT"},
        "entity_book_map": {"E1": "US BOOK"},
        "books": ["US BOOK"],
    }


def test_missing_entity_map_returns_hard_error(tmp_path: Path, log: logging.Logger, monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "fake-token-for-test")
    cfg = _base_config()
    cfg["entity_book_map"] = {}

    result = run_pipeline(
        config=cfg, out_dir=str(tmp_path), dry_run=True, log=log
    )

    assert result.exit_code == 2
    assert result.summary is None


def test_missing_books_returns_hard_error(tmp_path: Path, log: logging.Logger, monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "fake-token-for-test")
    cfg = _base_config()
    cfg["books"] = []

    result = run_pipeline(
        config=cfg, out_dir=str(tmp_path), dry_run=True, log=log
    )

    assert result.exit_code == 2


def test_pre_bip_enabled_without_requests_is_hard_error(
    tmp_path: Path, log: logging.Logger, monkeypatch
) -> None:
    monkeypatch.setenv("FUSION_JWT", "fake-token-for-test")
    cfg = _base_config()
    cfg["pre_bip_dff_updates"] = {
        "enabled": True,
        "operation_handle": "updateAssetDescriptiveDetails",
        "field_parameter_map": {"book_type_code": "P_BOOK_TYPE_CODE"},
        # no `requests` key — should be rejected
    }

    result = run_pipeline(
        config=cfg, out_dir=str(tmp_path), dry_run=True, log=log
    )

    assert result.exit_code == 2


def test_load_pre_bip_block_inline(log: logging.Logger) -> None:
    cfg = {"pre_bip_dff_updates": {"enabled": True, "field_parameter_map": {}}}
    block = _load_pre_bip_block(cfg, config_path=None, log=log)
    assert block == {"enabled": True, "field_parameter_map": {}}


def test_load_pre_bip_block_returns_empty_dict_when_neither_key_set(log: logging.Logger) -> None:
    block = _load_pre_bip_block({}, config_path=None, log=log)
    assert block == {}


def test_load_pre_bip_block_file_resolves_relative_to_config_path(
    tmp_path: Path, log: logging.Logger
) -> None:
    pre_block = {"enabled": True, "field_parameter_map": {}, "requests": []}
    pre_file = tmp_path / "pre_bip.json"
    pre_file.write_text(json.dumps(pre_block))

    config_path = tmp_path / "main_config.json"
    config_path.write_text("{}")

    cfg = {"pre_bip_dff_updates_file": "pre_bip.json"}
    block = _load_pre_bip_block(cfg, config_path=str(config_path), log=log)

    assert block == pre_block


def test_load_pre_bip_block_file_absolute_path_used_as_is(
    tmp_path: Path, log: logging.Logger
) -> None:
    pre_block = {"enabled": False}
    pre_file = tmp_path / "abs.json"
    pre_file.write_text(json.dumps(pre_block))

    cfg = {"pre_bip_dff_updates_file": str(pre_file)}
    block = _load_pre_bip_block(cfg, config_path=None, log=log)

    assert block == pre_block


def test_inline_block_used_when_file_key_absent(tmp_path: Path, log: logging.Logger) -> None:
    inline = {"enabled": False}
    cfg = {"pre_bip_dff_updates": inline}
    block = _load_pre_bip_block(cfg, config_path=str(tmp_path / "x.json"), log=log)
    assert block == inline
