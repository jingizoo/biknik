"""Tests for AccountCombinationServiceClient (SOAP validateAndCreateAccounts)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from ofam_asset_xfer.account_combination_client import (
    AccountCombinationServiceClient,
    AccountCombinationServiceConfig,
    CreatedCombination,
)
from ofam_asset_xfer.exceptions import FusionApiError

SUCCESS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header/>
  <env:Body>
    <ns0:validateAndCreateAccountsResponse
        xmlns:ns0="http://xmlns.oracle.com/apps/financials/generalLedger/accounts/codeCombinations/accountCombinationService/types/"
        xmlns:ns1="http://xmlns.oracle.com/apps/financials/generalLedger/accounts/codeCombinations/accountCombinationService/">
      <ns0:result>
        <ns1:CcId>12345678</ns1:CcId>
        <ns1:Status>S</ns1:Status>
        <ns1:ConcatenatedSegments>SG01-100-7000-PRD-NONE</ns1:ConcatenatedSegments>
      </ns0:result>
    </ns0:validateAndCreateAccountsResponse>
  </env:Body>
</env:Envelope>
"""


ERROR_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header/>
  <env:Body>
    <ns0:validateAndCreateAccountsResponse
        xmlns:ns0="http://xmlns.oracle.com/apps/financials/generalLedger/accounts/codeCombinations/accountCombinationService/types/"
        xmlns:ns1="http://xmlns.oracle.com/apps/financials/generalLedger/accounts/codeCombinations/accountCombinationService/">
      <ns0:result>
        <ns1:Status>E</ns1:Status>
        <ns1:ErrorCode>GL_INVALID_SEGMENT_VALUE</ns1:ErrorCode>
        <ns1:Error>Segment value 'XXX' is disabled or out of date range.</ns1:Error>
      </ns0:result>
    </ns0:validateAndCreateAccountsResponse>
  </env:Body>
</env:Envelope>
"""


SOAP_FAULT_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Body>
    <env:Fault>
      <faultcode>env:Server</faultcode>
      <faultstring>User does not have GL_MANAGE_GENERAL_LEDGER_ACCOUNT_COMBINATIONS_PRIV</faultstring>
    </env:Fault>
  </env:Body>
</env:Envelope>
"""


def _client(**overrides) -> AccountCombinationServiceClient:
    cfg = AccountCombinationServiceConfig(
        base_url="https://fusion.example.com",
        bearer_token="tok",
        **overrides,
    )
    return AccountCombinationServiceClient(cfg)


def _mock_session_post(client, *, status=200, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    client._session.post = MagicMock(return_value=resp)
    return resp


class TestConfigFromDict:
    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            AccountCombinationServiceConfig.from_dict({})

    def test_strips_trailing_slash(self):
        cfg = AccountCombinationServiceConfig.from_dict(
            {"base_url": "https://x/", "bearer_token": "t"}
        )
        assert cfg.base_url == "https://x"

    def test_reads_bearer_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "from-env")
        cfg = AccountCombinationServiceConfig.from_dict(
            {"base_url": "https://x", "bearer_token_env": "MY_TOKEN"}
        )
        assert cfg.bearer_token == "from-env"

    def test_defaults(self):
        cfg = AccountCombinationServiceConfig.from_dict(
            {"base_url": "https://x", "bearer_token": "t"}
        )
        assert cfg.endpoint_path == "/fscmService/AccountCombinationService"
        assert cfg.dynamic_insertion == "Yes"
        # EnabledFlag / FromDate / ToDate are off by default to match
        # Citadel's documented template; opt in via config or per-call kwargs.
        assert cfg.enabled_flag is None
        assert cfg.from_date is None
        assert cfg.to_date is None
        assert cfg.timeout_seconds == 60
        # Segment padding is off by default.
        assert cfg.pad_segments_to == 0
        assert cfg.pad_segments_with == "?"


class TestValidateAndCreate:
    def test_success_returns_ccid(self):
        client = _client()
        _mock_session_post(client, text=SUCCESS_RESPONSE)
        result = client.validate_and_create(
            ledger_name="Singapore Primary Ledger",
            segments={"Segment1": "SG01", "Segment2": "100"},
        )
        assert isinstance(result, CreatedCombination)
        assert result.ccid == 12345678
        assert result.status == "S"
        assert result.error_code is None
        assert result.concatenated_segments == "SG01-100-7000-PRD-NONE"

    def test_oracle_error_response_is_returned_not_raised(self):
        """Validation/cross-validation errors come back in the result
        body, not as exceptions.  Caller decides what to do."""
        client = _client()
        _mock_session_post(client, text=ERROR_RESPONSE)
        result = client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "XXX"},
        )
        assert result.ccid is None
        assert result.status == "E"
        assert result.error_code == "GL_INVALID_SEGMENT_VALUE"
        assert "disabled or out of date range" in (result.error_message or "")

    def test_http_error_raises(self):
        client = _client()
        _mock_session_post(client, status=401, text="Unauthorized")
        with pytest.raises(FusionApiError, match="HTTP 401"):
            client.validate_and_create(
                ledger_name="L",
                segments={"Segment1": "X"},
            )

    def test_empty_body_raises(self):
        client = _client()
        _mock_session_post(client, status=200, text="")
        with pytest.raises(FusionApiError, match="empty body"):
            client.validate_and_create(
                ledger_name="L",
                segments={"Segment1": "X"},
            )

    def test_transport_error_raises_fusion_error(self):
        client = _client()
        client._session.post = MagicMock(side_effect=requests.ConnectionError("boom"))
        with pytest.raises(FusionApiError, match="transport error"):
            client.validate_and_create(
                ledger_name="L",
                segments={"Segment1": "X"},
            )

    def test_soap_fault_raises(self):
        client = _client()
        _mock_session_post(client, text=SOAP_FAULT_RESPONSE)
        with pytest.raises(FusionApiError, match="SOAP fault"):
            client.validate_and_create(
                ledger_name="L",
                segments={"Segment1": "X"},
            )

    def test_missing_ledger_name_rejected_early(self):
        client = _client()
        with pytest.raises(ValueError, match="ledger_name"):
            client.validate_and_create(ledger_name="", segments={"Segment1": "X"})

    def test_empty_segments_rejected_early(self):
        client = _client()
        with pytest.raises(ValueError, match="segments"):
            client.validate_and_create(ledger_name="L", segments={})

    def test_envelope_matches_documented_template(self):
        """Citadel's documented SOAP template orders the row as:
        DynamicInsertion → Segment1..N → LedgerName (with EnabledFlag /
        FromDate as schema-optional, off by default).  Verify that's
        what we ship on the wire."""
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["url"] = url
            captured["body"] = data.decode("utf-8") if isinstance(data, bytes) else data
            captured["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="IN Primary Ledger",
            segments={
                "Segment2": "125",
                "Segment1": "3004",
                "Segment10": "00000",
                "Segment3": "10510000",
            },
        )

        body = captured["body"]
        # All elements present
        assert "<acc:LedgerName>IN Primary Ledger</acc:LedgerName>" in body
        assert "<acc:Segment1>3004</acc:Segment1>" in body
        assert "<acc:Segment2>125</acc:Segment2>" in body
        assert "<acc:Segment3>10510000</acc:Segment3>" in body
        assert "<acc:Segment10>00000</acc:Segment10>" in body
        assert "<acc:DynamicInsertion>Yes</acc:DynamicInsertion>" in body
        # Documented order: DynamicInsertion → Segment1..N → LedgerName.
        di_pos = body.index("<acc:DynamicInsertion>")
        seg1_pos = body.index("<acc:Segment1>")
        seg2_pos = body.index("<acc:Segment2>")
        seg10_pos = body.index("<acc:Segment10>")
        ledger_pos = body.index("<acc:LedgerName>")
        assert di_pos < seg1_pos < seg2_pos < seg10_pos < ledger_pos
        # Schema-optional tail elements are OFF by default — must not appear.
        assert "<acc:FromDate>" not in body
        assert "<acc:EnabledFlag>" not in body
        # URL hit
        assert captured["url"] == "https://fusion.example.com/fscmService/AccountCombinationService"
        # Auth + SOAPAction headers
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["headers"]["SOAPAction"] == "validateAndCreateAccounts"

    def test_xml_special_chars_in_segments_are_escaped(self):
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="A & B",
            segments={"Segment1": "<X>"},
        )
        assert "A &amp; B" in captured["body"]
        assert "&lt;X&gt;" in captured["body"]

    def test_dynamic_insertion_can_be_overridden_per_call(self):
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "X"},
            dynamic_insertion="No",  # pure validation
        )
        assert "<acc:DynamicInsertion>No</acc:DynamicInsertion>" in captured["body"]

    def test_optional_tail_elements_emitted_when_explicitly_set(self):
        """``EnabledFlag`` and ``FromDate`` are schema-optional.  They
        must NOT appear by default (matches Citadel's documented
        template), but DO appear when the caller passes them."""
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "X"},
            from_date="2026-05-14",
            enabled_flag="true",
        )
        body = captured["body"]
        assert "<acc:FromDate>2026-05-14</acc:FromDate>" in body
        assert "<acc:EnabledFlag>true</acc:EnabledFlag>" in body
        # Tail elements come after LedgerName per the documented template.
        assert body.index("<acc:LedgerName>") < body.index("<acc:FromDate>")

    def test_to_date_element_emitted_when_set(self):
        """ToDate is a schema-optional tail element (Oracle support
        docs show it alongside FromDate); emitted only when set."""
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "X"},
            from_date="2017-10-17",
            to_date="2023-10-22",
        )
        body = captured["body"]
        assert "<acc:FromDate>2017-10-17</acc:FromDate>" in body
        assert "<acc:ToDate>2023-10-22</acc:ToDate>" in body
        # ToDate comes after FromDate in the documented order.
        assert body.index("<acc:FromDate>") < body.index("<acc:ToDate>")

    def test_pad_segments_emits_placeholder_for_missing_slots(self):
        """When pad_segments_to=N is set, slots not in the caller's
        segments map are emitted as <acc:SegmentK>?</acc:SegmentK>."""
        cfg = AccountCombinationServiceConfig.from_dict(
            {
                "base_url": "https://x",
                "bearer_token": "t",
                "pad_segments_to": 5,
                "pad_segments_with": "?",
            }
        )
        client = AccountCombinationServiceClient(cfg)
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "01", "Segment3": "2580"},  # 2, 4, 5 missing
        )
        body = captured["body"]
        # Explicit values preserved
        assert "<acc:Segment1>01</acc:Segment1>" in body
        assert "<acc:Segment3>2580</acc:Segment3>" in body
        # Missing slots padded with "?"
        assert "<acc:Segment2>?</acc:Segment2>" in body
        assert "<acc:Segment4>?</acc:Segment4>" in body
        assert "<acc:Segment5>?</acc:Segment5>" in body

    def test_pad_segments_off_by_default(self):
        client = _client()
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(
            ledger_name="L",
            segments={"Segment1": "01", "Segment3": "2580"},
        )
        body = captured["body"]
        # Missing slots are NOT emitted by default.
        assert "<acc:Segment2>" not in body
        assert "<acc:Segment4>" not in body

    def test_optional_tail_elements_can_be_configured_globally(self):
        """Setting ``enabled_flag`` / ``from_date`` on the config makes
        every call include them — handy for pods that require either."""
        cfg = AccountCombinationServiceConfig.from_dict(
            {
                "base_url": "https://x",
                "bearer_token": "t",
                "enabled_flag": "true",
                "from_date": "2026-05-14",
            }
        )
        client = AccountCombinationServiceClient(cfg)
        captured = {}

        def _capture(url, data=None, headers=None, **kwargs):
            captured["body"] = data.decode("utf-8")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = SUCCESS_RESPONSE
            return resp

        client._session.post = MagicMock(side_effect=_capture)
        client.validate_and_create(ledger_name="L", segments={"Segment1": "X"})
        body = captured["body"]
        assert "<acc:FromDate>2026-05-14</acc:FromDate>" in body
        assert "<acc:EnabledFlag>true</acc:EnabledFlag>" in body


class TestCreatorFunctionalIntegrationWithResolver:
    """Sanity-check that ccid_resolver's create-on-miss path calls the
    creator with the expected signature."""

    def test_resolver_calls_creator_when_lookup_misses(self):
        from ofam_asset_xfer.ccid_resolver import lookup_ccid_by_segments

        rest_client = MagicMock()
        rest_client.get_resource.return_value = {"items": []}

        captured_calls = []

        def _creator(ledger_name, segments):
            captured_calls.append((ledger_name, dict(segments)))
            return 999000

        ccid = lookup_ccid_by_segments(
            rest_client,
            {"Segment1": "SG01", "Segment2": "100"},
            chart_of_accounts_id=42,
            creator=_creator,
            ledger_name="Singapore Primary Ledger",
        )

        assert ccid == 999000
        assert len(captured_calls) == 1
        ledger, segs = captured_calls[0]
        assert ledger == "Singapore Primary Ledger"
        assert segs == {"Segment1": "SG01", "Segment2": "100"}

    def test_resolver_falls_back_to_error_when_no_creator(self):
        from ofam_asset_xfer.ccid_resolver import lookup_ccid_by_segments
        from ofam_asset_xfer.exceptions import ValidationError

        rest_client = MagicMock()
        rest_client.get_resource.return_value = {"items": []}

        with pytest.raises(ValidationError, match="No account combination found"):
            lookup_ccid_by_segments(
                rest_client,
                {"Segment1": "SG01"},
                chart_of_accounts_id=42,
            )

    def test_resolver_propagates_fusion_error_from_creator(self):
        from ofam_asset_xfer.ccid_resolver import lookup_ccid_by_segments

        rest_client = MagicMock()
        rest_client.get_resource.return_value = {"items": []}

        def _bad_creator(ledger_name, segments):
            raise FusionApiError("cross-validation rule violation")

        with pytest.raises(FusionApiError, match="cross-validation"):
            lookup_ccid_by_segments(
                rest_client,
                {"Segment1": "X"},
                chart_of_accounts_id=42,
                creator=_bad_creator,
                ledger_name="L",
            )
