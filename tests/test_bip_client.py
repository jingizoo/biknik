"""Unit tests for ofam_asset_xfer.bip_client (BI Publisher SOAP client).

All tests use mock HTTP responses — no network calls.
"""

from __future__ import annotations

import base64
import pytest
from unittest.mock import MagicMock

from ofam_asset_xfer.bip_client import BIPClient, BIPConfig, _xml_escape
from ofam_asset_xfer.exceptions import FusionApiError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides):
    defaults = {
        "base_url": "https://fa-test.oraclecloud.com",
        "bearer_token": "test-token-123",
        "report_path": "/Custom/Integrations/Outbound/FA/TransferDFF.xdo",
    }
    defaults.update(overrides)
    return BIPConfig(**defaults)


def _report_xml(*rows):
    """Build a DATA_DS XML document with G_1 rows."""
    g1_blocks = []
    for row in rows:
        children = "".join(f"<{k}>{v}</{k}>" for k, v in row.items())
        g1_blocks.append(f"<G_1>{children}</G_1>")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><DATA_DS>{"".join(g1_blocks)}</DATA_DS>'
    )


def _soap_response(report_xml_str: str) -> bytes:
    """Wrap report XML in a SOAP runReportResponse envelope."""
    b64 = base64.b64encode(report_xml_str.encode("utf-8")).decode("ascii")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:ns2="http://xmlns.oracle.com/oxp/service/PublicReportService">'
        "<env:Body>"
        "<ns2:runReportResponse>"
        "<ns2:runReportReturn>"
        f"<ns2:reportBytes>{b64}</ns2:reportBytes>"
        "<ns2:reportContentType>application/xml</ns2:reportContentType>"
        "</ns2:runReportReturn>"
        "</ns2:runReportResponse>"
        "</env:Body>"
        "</env:Envelope>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# BIPConfig
# ---------------------------------------------------------------------------


class TestBIPConfig:
    def test_from_dict(self):
        cfg = BIPConfig.from_dict(
            {
                "base_url": "https://host.example.com/",
                "bearer_token": "tok",
                "report_path": "/Custom/Report.xdo",
            }
        )
        assert cfg.base_url == "https://host.example.com"
        assert cfg.bearer_token == "tok"
        assert cfg.report_path == "/Custom/Report.xdo"

    def test_from_dict_env_token(self, monkeypatch):
        monkeypatch.setenv("MY_BIP_TOKEN", "env-secret")
        cfg = BIPConfig.from_dict(
            {
                "base_url": "https://host.example.com",
                "bearer_token_env": "MY_BIP_TOKEN",
                "report_path": "/Custom/Report.xdo",
            }
        )
        assert cfg.bearer_token == "env-secret"

    def test_from_dict_missing_url(self):
        with pytest.raises(ValueError, match="base_url"):
            BIPConfig.from_dict({"bearer_token": "x", "report_path": "/x"})

    def test_from_dict_missing_token(self):
        with pytest.raises(ValueError, match="bearer_token"):
            BIPConfig.from_dict({"base_url": "https://x", "report_path": "/x"})

    def test_from_dict_missing_report_path(self):
        with pytest.raises(ValueError, match="report_path"):
            BIPConfig.from_dict({"base_url": "https://x", "bearer_token": "y"})


# ---------------------------------------------------------------------------
# BIPClient._extract_report_bytes
# ---------------------------------------------------------------------------


class TestExtractReportBytes:
    def test_extracts_base64_content(self):
        xml_str = _report_xml({"COL1": "val1"})
        soap = _soap_response(xml_str)
        result = BIPClient._extract_report_bytes(soap)
        assert result == xml_str.encode("utf-8")

    def test_raises_when_no_report_bytes(self):
        empty_soap = (
            b'<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope">'
            b'<env:Body><ns2:runReportResponse xmlns:ns2="http://xmlns.oracle.com/oxp/service/PublicReportService">'
            b"<ns2:runReportReturn></ns2:runReportReturn>"
            b"</ns2:runReportResponse></env:Body></env:Envelope>"
        )
        with pytest.raises(FusionApiError, match="No reportBytes"):
            BIPClient._extract_report_bytes(empty_soap)


# ---------------------------------------------------------------------------
# BIPClient._parse_data_ds
# ---------------------------------------------------------------------------


class TestParseDataDs:
    def test_parses_single_row(self):
        xml = _report_xml({"ASSET_NUMBER": "142847", "BOOK_TYPE_CODE": "US CORP BOOK"})
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert len(rows) == 1
        assert rows[0]["ASSET_NUMBER"] == "142847"
        assert rows[0]["BOOK_TYPE_CODE"] == "US CORP BOOK"

    def test_parses_multiple_rows(self):
        xml = _report_xml(
            {"ASSET_NUMBER": "100", "STATUS": "ACTIVE"},
            {"ASSET_NUMBER": "200", "STATUS": "RETIRED"},
        )
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert len(rows) == 2
        assert rows[0]["ASSET_NUMBER"] == "100"
        assert rows[1]["ASSET_NUMBER"] == "200"

    def test_empty_report(self):
        xml = '<?xml version="1.0"?><DATA_DS></DATA_DS>'
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert rows == []

    def test_handles_empty_element_text(self):
        xml = _report_xml({"ASSET_NUMBER": "100", "EMPTY_COL": ""})
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert rows[0]["EMPTY_COL"] == ""

    def test_strips_whitespace(self):
        xml = "<DATA_DS><G_1><ASSET_NUMBER>  142847  </ASSET_NUMBER></G_1></DATA_DS>"
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert rows[0]["ASSET_NUMBER"] == "142847"

    def test_handles_bom(self):
        xml = _report_xml({"COL": "val"})
        data = b"\xef\xbb\xbf" + xml.encode("utf-8")
        rows = BIPClient._parse_data_ds(data)
        assert len(rows) == 1

    def test_root_level_elements_injected_into_rows(self):
        """Root-level elements like P_BOOK_TYPE_CODE should appear in every row."""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<DATA_DS>"
            "<P_BOOK_TYPE_CODE>UK CORP BOOK</P_BOOK_TYPE_CODE>"
            "<G_1><ASSET_NUMBER>100</ASSET_NUMBER></G_1>"
            "<G_1><ASSET_NUMBER>200</ASSET_NUMBER></G_1>"
            "</DATA_DS>"
        )
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert len(rows) == 2
        assert rows[0]["P_BOOK_TYPE_CODE"] == "UK CORP BOOK"
        assert rows[1]["P_BOOK_TYPE_CODE"] == "UK CORP BOOK"
        assert rows[0]["ASSET_NUMBER"] == "100"
        assert rows[1]["ASSET_NUMBER"] == "200"

    def test_g1_field_overrides_root_field(self):
        """If a G_1 element has the same name as a root element, G_1 wins."""
        xml = (
            "<DATA_DS>"
            "<BOOK>ROOT_VAL</BOOK>"
            "<G_1><BOOK>ROW_VAL</BOOK></G_1>"
            "</DATA_DS>"
        )
        rows = BIPClient._parse_data_ds(xml.encode("utf-8"))
        assert rows[0]["BOOK"] == "ROW_VAL"


# ---------------------------------------------------------------------------
# BIPClient.run_report (integration with mock HTTP)
# ---------------------------------------------------------------------------


class TestRunReport:
    def test_run_report_success(self):
        report_xml = _report_xml(
            {"ASSET_NUMBER": "142847", "ATTRIBUTE9": "UK Entity"},
        )
        soap = _soap_response(report_xml)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = soap

        client = BIPClient(_cfg())
        client._session = MagicMock()
        client._session.post.return_value = mock_resp

        rows = client.run_report(params={"P_BOOK": "US CORP BOOK"})

        assert len(rows) == 1
        assert rows[0]["ASSET_NUMBER"] == "142847"
        assert rows[0]["ATTRIBUTE9"] == "UK Entity"

        # Verify SOAP call was made correctly
        call_args = client._session.post.call_args
        assert "ExternalReportWSSService" in call_args[0][0]

    def test_run_report_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        client = BIPClient(_cfg())
        client._session = MagicMock()
        client._session.post.return_value = mock_resp

        with pytest.raises(FusionApiError, match="HTTP 500"):
            client.run_report()

    def test_run_report_with_override_path(self):
        report_xml = _report_xml({"COL": "val"})
        soap = _soap_response(report_xml)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = soap

        client = BIPClient(_cfg())
        client._session = MagicMock()
        client._session.post.return_value = mock_resp

        client.run_report(report_path="/Custom/Other/Report.xdo")

        call_data = (
            client._session.post.call_args[1].get("data")
            or client._session.post.call_args[0][1]
            if len(client._session.post.call_args[0]) > 1
            else None
        )
        if call_data is None:
            call_data = client._session.post.call_args[1]["data"]
        assert b"/Custom/Other/Report.xdo" in call_data

    def test_soap_envelope_includes_params(self):
        report_xml = _report_xml()
        soap = _soap_response(report_xml)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = soap

        client = BIPClient(_cfg())
        client._session = MagicMock()
        client._session.post.return_value = mock_resp

        client.run_report(params={"P_BOOK_TYPE_CODE": "US CORP BOOK"})

        call_data = client._session.post.call_args[1]["data"]
        assert b"P_BOOK_TYPE_CODE" in call_data
        assert b"US CORP BOOK" in call_data

    def test_soap_envelope_escapes_special_chars(self):
        report_xml = _report_xml()
        soap = _soap_response(report_xml)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = soap

        client = BIPClient(_cfg())
        client._session = MagicMock()
        client._session.post.return_value = mock_resp

        client.run_report(params={"P_FILTER": "A & B < C"})

        call_data = client._session.post.call_args[1]["data"]
        assert b"A &amp; B &lt; C" in call_data


# ---------------------------------------------------------------------------
# _xml_escape
# ---------------------------------------------------------------------------


class TestXmlEscape:
    def test_escapes_ampersand(self):
        assert _xml_escape("a & b") == "a &amp; b"

    def test_escapes_lt_gt(self):
        assert _xml_escape("<tag>") == "&lt;tag&gt;"

    def test_escapes_quotes(self):
        assert _xml_escape('say "hi"') == "say &quot;hi&quot;"

    def test_no_change_for_plain_text(self):
        assert _xml_escape("plain text") == "plain text"
