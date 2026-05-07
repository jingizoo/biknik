"""Tests for oracle_client response parsing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ofam_asset_xfer.exceptions import FusionApiError
from ofam_asset_xfer.oracle_client import _parse_fusion_response


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
