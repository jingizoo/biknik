"""Tests for oracle_client response parsing and debug dumping."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ofam_asset_xfer.exceptions import FusionApiError
from ofam_asset_xfer.oracle_client import (
    OracleConfig,
    OracleErpIntegrationsClient,
    _parse_fusion_response,
    _redact_headers,
)


def _resp(status: int, text: str = "", json_value=None, raise_json: bool = False):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if raise_json:
        r.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    else:
        r.json.return_value = json_value
    return r


class TestParseFusionResponse:
    def test_returns_parsed_json_on_success(self) -> None:
        r = _resp(200, text='{"a": 1}', json_value={"a": 1})
        assert _parse_fusion_response(r, context="POST x") == {"a": 1}

    def test_http_error_takes_precedence_over_json_parse(self) -> None:
        # 502 with empty body — must surface HTTP status, not JSONDecodeError
        r = _resp(502, text="", raise_json=True)
        with pytest.raises(FusionApiError, match=r"HTTP 502"):
            _parse_fusion_response(r, context="POST processTransaction-transferAsset")

    def test_http_error_with_html_body(self) -> None:
        r = _resp(401, text="<html>Unauthorized</html>", raise_json=True)
        with pytest.raises(FusionApiError, match=r"HTTP 401"):
            _parse_fusion_response(r, context="GET foo")

    def test_empty_body_with_2xx_status(self) -> None:
        r = _resp(200, text="", raise_json=True)
        with pytest.raises(FusionApiError, match=r"empty body"):
            _parse_fusion_response(r, context="POST x")

    def test_whitespace_body_is_treated_as_empty(self) -> None:
        r = _resp(200, text="   \n  ", raise_json=True)
        with pytest.raises(FusionApiError, match=r"empty body"):
            _parse_fusion_response(r, context="POST x")

    def test_non_json_body_with_2xx_status(self) -> None:
        r = _resp(200, text="<html>Welcome</html>", raise_json=True)
        with pytest.raises(FusionApiError, match=r"non-JSON response"):
            _parse_fusion_response(r, context="POST x")

    def test_non_dict_json_payload(self) -> None:
        r = _resp(200, text="[1,2,3]", json_value=[1, 2, 3])
        with pytest.raises(FusionApiError, match=r"unexpected JSON shape"):
            _parse_fusion_response(r, context="POST x")

    def test_error_message_includes_context(self) -> None:
        r = _resp(500, text="boom", raise_json=True)
        with pytest.raises(FusionApiError, match=r"POST processTransaction-transferAsset"):
            _parse_fusion_response(r, context="POST processTransaction-transferAsset")

    def test_error_message_truncates_long_body(self) -> None:
        r = _resp(500, text="x" * 5000, raise_json=True)
        with pytest.raises(FusionApiError) as ei:
            _parse_fusion_response(r, context="GET foo")
        # Should not include the full 5000-char body
        assert len(str(ei.value)) < 1000


class TestRedactHeaders:
    def test_authorization_redacted(self) -> None:
        out = _redact_headers(
            {"Authorization": "Bearer secret-jwt", "Content-Type": "application/json"}
        )
        assert out["Authorization"] == "***REDACTED***"
        assert out["Content-Type"] == "application/json"

    def test_case_insensitive(self) -> None:
        out = _redact_headers({"authorization": "x", "Cookie": "y", "X-API-Key": "z"})
        assert out["authorization"] == "***REDACTED***"
        assert out["Cookie"] == "***REDACTED***"
        assert out["X-API-Key"] == "***REDACTED***"

    def test_non_dict_input_safe(self) -> None:
        assert _redact_headers(None) == {}


def _build_client(debug_dir: Path | None = None) -> OracleErpIntegrationsClient:
    cfg = OracleConfig(
        base_url="https://fusion.example.com",
        api_version="11.13.18.05",
        bearer_token="tok",
    )
    return OracleErpIntegrationsClient(cfg, debug_dir=debug_dir)


def _mock_post_response(client: OracleErpIntegrationsClient, *, status: int, text: str, json_value):
    fake_resp = MagicMock()
    fake_resp.status_code = status
    fake_resp.text = text
    fake_resp.headers = {"Content-Type": "application/json"}
    fake_resp.json.return_value = json_value
    client._session.post = MagicMock(return_value=fake_resp)  # type: ignore[assignment]
    client._session.get = MagicMock(return_value=fake_resp)  # type: ignore[assignment]
    return fake_resp


class TestDebugDump:
    def test_no_dump_when_debug_dir_unset(self, tmp_path: Path) -> None:
        client = _build_client(debug_dir=None)
        _mock_post_response(client, status=200, text='{"x":1}', json_value={"x": 1})
        client.process_transaction("transferAsset", {"P_ASSET_NUMBER": "100"})
        assert list(tmp_path.iterdir()) == []  # nothing written

    def test_dump_written_for_post(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dumps"
        client = _build_client(debug_dir=dump_dir)
        _mock_post_response(
            client,
            status=200,
            text='{"ParameterList":"{}"}',
            json_value={"ParameterList": "{}"},
        )

        client.process_transaction("transferAsset", {"P_ASSET_NUMBER": "100"})

        files = sorted(dump_dir.glob("*.json"))
        assert len(files) == 1
        # Filename should include the handle for findability.
        assert "processTransaction-transferAsset" in files[0].name
        payload = json.loads(files[0].read_text())
        assert payload["request"]["method"] == "POST"
        assert "erpintegrations" in payload["request"]["url"]
        assert payload["request"]["body"]["OperationName"] == "processTransaction-transferAsset"
        assert payload["response"]["status_code"] == 200
        # Auth header must be redacted
        assert payload["request"]["headers"]["Authorization"] == "***REDACTED***"

    def test_dump_written_for_get(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dumps"
        client = _build_client(debug_dir=dump_dir)
        _mock_post_response(client, status=200, text='{"items":[]}', json_value={"items": []})

        client.get_resource("accountCombinationsLOV", {"q": "x"})

        files = sorted(dump_dir.glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["request"]["method"] == "GET"
        assert "accountCombinationsLOV" in payload["request"]["url"]
        assert payload["request"]["body"]["query_params"] == {"q": "x"}

    def test_sequence_increments(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dumps"
        client = _build_client(debug_dir=dump_dir)
        _mock_post_response(client, status=200, text="{}", json_value={})

        client.process_transaction("transferAsset", {})
        client.process_transaction("bookTransfer", {})

        files = sorted(dump_dir.glob("*.json"))
        assert len(files) == 2
        seqs = [json.loads(f.read_text())["sequence"] for f in files]
        assert seqs == [1, 2]

    def test_dump_written_on_transport_error(self, tmp_path: Path) -> None:
        import requests as _requests

        dump_dir = tmp_path / "dumps"
        client = _build_client(debug_dir=dump_dir)
        client._session.post = MagicMock(  # type: ignore[assignment]
            side_effect=_requests.ConnectionError("connection refused")
        )

        with pytest.raises(_requests.ConnectionError):
            client.process_transaction("transferAsset", {})

        files = sorted(dump_dir.glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["error"].startswith("ConnectionError")
        assert "response" not in payload

    def test_dump_failure_does_not_break_request(self, tmp_path: Path) -> None:
        dump_dir = tmp_path / "dumps"
        client = _build_client(debug_dir=dump_dir)
        _mock_post_response(client, status=200, text="{}", json_value={})

        # Force the dumper write to fail mid-call: dump() should swallow it.
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            raw, pl = client.process_transaction("transferAsset", {})

        # Request still returns successfully even though dump failed.
        assert raw == {}
        assert pl == {}
