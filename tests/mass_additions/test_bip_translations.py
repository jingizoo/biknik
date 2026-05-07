"""Tests for the BIP translations loader."""
from __future__ import annotations

import base64
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.cmdb.bip_translations import (
    BipTranslationsConfig,
    BipTranslationsLoader,
    _parse_g1_rows,
    _xml_escape,
)
from ofam_mass_additions.exceptions import ConfigError


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_cfg(**kwargs) -> BipTranslationsConfig:
    defaults = {
        "base_url": "https://fa.example.com",
        "report_path": "/Custom/FA/Translations.xdo",
        "bearer_token_env": "FUSION_TOKEN",
    }
    return BipTranslationsConfig.from_dict({**defaults, **kwargs})


def _wrap_report_bytes(xml_body: str) -> bytes:
    """Wrap DATA_DS XML in a fake SOAP response with base64 reportBytes."""
    encoded = base64.b64encode(xml_body.encode("utf-8")).decode("ascii")
    return (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
        "<soap:Body>"
        "<runReportResponse xmlns=\"http://xmlns.oracle.com/oxp/service/PublicReportService\">"
        f"<reportBytes>{encoded}</reportBytes>"
        "</runReportResponse>"
        "</soap:Body>"
        "</soap:Envelope>"
    ).encode("utf-8")


def _ok_response(xml_body: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = _wrap_report_bytes(xml_body)
    return resp


SAMPLE_XML = dedent("""\
    <DATA_DS>
      <G_1>
        <TRANSLATION_FIELD>location_id</TRANSLATION_FIELD>
        <CMDB_CODE>LC000030</CMDB_CODE>
        <ORACLE_FA_ID>300000004974106</ORACLE_FA_ID>
      </G_1>
      <G_1>
        <TRANSLATION_FIELD>expense_ccid</TRANSLATION_FIELD>
        <CMDB_CODE>30755</CMDB_CODE>
        <ORACLE_FA_ID>682610</ORACLE_FA_ID>
      </G_1>
      <G_1>
        <TRANSLATION_FIELD>employee_id</TRANSLATION_FIELD>
        <CMDB_CODE>E12345</CMDB_CODE>
        <ORACLE_FA_ID>99001</ORACLE_FA_ID>
      </G_1>
    </DATA_DS>
""")


# ── BipTranslationsConfig ────────────────────────────────────────────────────


def test_from_dict_requires_base_url() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        BipTranslationsConfig.from_dict({"report_path": "/x.xdo"})


def test_from_dict_requires_report_path() -> None:
    with pytest.raises(ConfigError, match="report_path"):
        BipTranslationsConfig.from_dict({"base_url": "https://fa.example.com"})


def test_from_dict_defaults() -> None:
    cfg = _make_cfg()
    assert cfg.book_type_code == "US CORP BOOK"
    assert cfg.verify_ssl is True
    assert cfg.timeout_seconds == 120


# ── _parse_g1_rows ───────────────────────────────────────────────────────────


def test_parse_g1_rows_all_fields() -> None:
    rows = _parse_g1_rows(SAMPLE_XML.encode("utf-8"))
    assert len(rows) == 3
    assert rows[0] == {
        "TRANSLATION_FIELD": "location_id",
        "CMDB_CODE": "LC000030",
        "ORACLE_FA_ID": "300000004974106",
    }


def test_parse_g1_rows_empty() -> None:
    assert _parse_g1_rows(b"<DATA_DS/>") == []


# ── BipTranslationsLoader.load ───────────────────────────────────────────────


def test_load_builds_translations_dict(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_TOKEN", "tok")
    cfg = _make_cfg()
    loader = BipTranslationsLoader(cfg)
    with patch.object(loader._session, "post", return_value=_ok_response(SAMPLE_XML)):
        result = loader.load()

    assert result["location_id"] == {"LC000030": 300000004974106}
    assert result["expense_ccid"] == {"30755": 682610}
    assert result["employee_id"] == {"E12345": 99001}


def test_load_logs_raw_xml_when_zero_rows(monkeypatch, caplog) -> None:
    """When the BIP report returns 0 G_1 rows, dump the raw XML so the
    operator can see what the report actually produced."""
    import logging

    monkeypatch.setenv("FUSION_TOKEN", "tok")
    empty_xml = "<DATA_DS><P_BOOK_TYPE_CODE>US CORP BOOK</P_BOOK_TYPE_CODE></DATA_DS>"
    loader = BipTranslationsLoader(_make_cfg())
    with patch.object(loader._session, "post", return_value=_ok_response(empty_xml)):
        with caplog.at_level(
            logging.WARNING, logger="ofam_mass_additions.cmdb.bip_translations"
        ):
            result = loader.load()

    assert result == {"location_id": {}, "expense_ccid": {}, "employee_id": {}}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("0 G_1 rows" in m and "P_BOOK_TYPE_CODE" in m for m in msgs)


def test_load_skips_unknown_field(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_TOKEN", "tok")
    xml = dedent("""\
        <DATA_DS>
          <G_1>
            <TRANSLATION_FIELD>unknown_field</TRANSLATION_FIELD>
            <CMDB_CODE>X</CMDB_CODE>
            <ORACLE_FA_ID>1</ORACLE_FA_ID>
          </G_1>
        </DATA_DS>
    """)
    loader = BipTranslationsLoader(_make_cfg())
    with patch.object(loader._session, "post", return_value=_ok_response(xml)):
        result = loader.load()

    assert result == {"location_id": {}, "expense_ccid": {}, "employee_id": {}}


def test_load_skips_non_integer_oracle_id(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setenv("FUSION_TOKEN", "tok")
    xml = dedent("""\
        <DATA_DS>
          <G_1>
            <TRANSLATION_FIELD>location_id</TRANSLATION_FIELD>
            <CMDB_CODE>LC000030</CMDB_CODE>
            <ORACLE_FA_ID>not-a-number</ORACLE_FA_ID>
          </G_1>
        </DATA_DS>
    """)
    loader = BipTranslationsLoader(_make_cfg())
    with patch.object(loader._session, "post", return_value=_ok_response(xml)):
        with caplog.at_level(logging.WARNING, logger="ofam_mass_additions.cmdb.bip_translations"):
            result = loader.load()

    assert result["location_id"] == {}
    assert any("non-integer" in r.getMessage() for r in caplog.records)


def test_load_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_TOKEN", "tok")
    resp = MagicMock(status_code=401, text="Unauthorized")
    loader = BipTranslationsLoader(_make_cfg())
    with patch.object(loader._session, "post", return_value=resp):
        with pytest.raises(ConfigError, match="HTTP 401"):
            loader.load()


def test_auth_uses_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_TOKEN", "my-jwt")
    loader = BipTranslationsLoader(_make_cfg())
    headers = loader._auth_headers()
    assert headers == {"Authorization": "Bearer my-jwt"}


def test_auth_uses_username_password(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_TOKEN", raising=False)
    monkeypatch.setenv("FUSION_PW", "s3cr3t")
    cfg = BipTranslationsConfig.from_dict(
        {
            "base_url": "https://fa.example.com",
            "report_path": "/x.xdo",
            "username": "svc_user",
            "password_env": "FUSION_PW",
        }
    )
    loader = BipTranslationsLoader(cfg)
    headers = loader._auth_headers()
    expected = base64.b64encode(b"svc_user:s3cr3t").decode("ascii")
    assert headers == {"Authorization": f"Basic {expected}"}


def test_auth_raises_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_TOKEN", raising=False)
    cfg = BipTranslationsConfig.from_dict(
        {
            "base_url": "https://fa.example.com",
            "report_path": "/x.xdo",
            "bearer_token_env": "FUSION_TOKEN",
        }
    )
    loader = BipTranslationsLoader(cfg)
    with pytest.raises(ConfigError, match="no auth configured"):
        loader._auth_headers()


def test_token_provider_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_TOKEN", "static-token")
    loader = BipTranslationsLoader(_make_cfg(), token_provider=lambda: "dynamic-token")
    headers = loader._auth_headers()
    assert headers == {"Authorization": "Bearer dynamic-token"}


# ── _xml_escape ───────────────────────────────────────────────────────────────


def test_xml_escape_special_chars() -> None:
    assert _xml_escape("a&b<c>d\"e'f") == "a&amp;b&lt;c&gt;d&quot;e&apos;f"
    assert _xml_escape("plain") == "plain"
