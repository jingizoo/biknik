"""Tests for the real Fusion FA mass-additions client.

Both reads and writes go through ``processTransaction`` on
``/erpintegrations`` (Oracle has no native REST resource for mass
additions), so all HTTP traffic in these tests is mocked at
``requests.Session.post`` and the test asserts on the OperationName +
ParameterList shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.exceptions import ConfigError, FusionApiError
from ofam_mass_additions.oracle.discovery import (
    BipReportDiscovery,
    ExplicitIdsDiscovery,
    build_discovery,
)
from ofam_mass_additions.oracle.fusion_client import (
    FusionFaClient,
    FusionFaConfig,
    _parse_parameter_list,
)


def _ok(body: dict) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _err(status: int, text: str = "boom") -> MagicMock:
    return MagicMock(status_code=status, text=text)


def _config(**overrides) -> FusionFaConfig:
    base = dict(
        base_url="https://fa.example.com",
        api_version="11.13.18.05",
        bearer_token_env="FUSION_JWT",
        verify_ssl=True,
        require_proxy=False,
    )
    base.update(overrides)
    return FusionFaConfig(**base)


def _client(monkeypatch, **discovery_kw) -> FusionFaClient:
    monkeypatch.setenv("FUSION_JWT", "tok")
    discovery = ExplicitIdsDiscovery.from_dict(
        discovery_kw or {"ids": ["1"]}
    )
    return FusionFaClient(_config(), discovery=discovery)


# ============================================================================
# FusionFaConfig.from_dict
# ============================================================================
def test_from_dict_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        FusionFaConfig.from_dict({})


def test_from_dict_strips_trailing_slash() -> None:
    cfg = FusionFaConfig.from_dict(
        {"base_url": "https://fa.example.com/", "api_version": "11.13.18.05"}
    )
    assert cfg.base_url == "https://fa.example.com"


# ============================================================================
# Auth
# ============================================================================
def test_missing_bearer_raises(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_JWT", raising=False)
    client = FusionFaClient(_config(), discovery=ExplicitIdsDiscovery(ids=("1",)))
    with pytest.raises(FusionApiError, match="No Fusion JWT available"):
        client._auth_headers()


def test_bearer_header_built(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config(), discovery=ExplicitIdsDiscovery(ids=("1",)))
    assert client._auth_headers()["Authorization"] == "Bearer tok"


# ============================================================================
# Discovery wiring (delegates to MassAdditionDiscovery)
# ============================================================================
def test_list_delegates_to_discovery(monkeypatch) -> None:
    """The client must defer entirely to the injected discovery."""
    monkeypatch.setenv("FUSION_JWT", "tok")
    discovery = MagicMock(spec=ExplicitIdsDiscovery)
    discovery.list_new_mass_addition_ids.return_value = ["100", "101"]

    client = FusionFaClient(_config(), discovery=discovery)
    ids = client.list_new_mass_addition_ids(book_type_code="US CORP BOOK")

    assert ids == ["100", "101"]
    discovery.list_new_mass_addition_ids.assert_called_once_with(
        book_type_code="US CORP BOOK"
    )


def test_explicit_ids_per_book_overrides_flat() -> None:
    d = ExplicitIdsDiscovery.from_dict(
        {
            "ids": ["999"],
            "ids_by_book": {"US CORP BOOK": ["1", "2"], "UK CORP BOOK": ["3"]},
        }
    )
    assert d.list_new_mass_addition_ids(book_type_code="US CORP BOOK") == ["1", "2"]
    assert d.list_new_mass_addition_ids(book_type_code="UK CORP BOOK") == ["3"]
    # Books not in the per-book map fall through to the flat list:
    assert d.list_new_mass_addition_ids(book_type_code="OTHER") == ["999"]


def test_explicit_ids_requires_at_least_one() -> None:
    with pytest.raises(ConfigError, match="ids"):
        ExplicitIdsDiscovery.from_dict({})


def test_bip_discovery_raises_until_implemented() -> None:
    d = BipReportDiscovery.from_dict({"report_path": "/x"})
    with pytest.raises(ConfigError, match="not yet wired"):
        d.list_new_mass_addition_ids(book_type_code="X")


def test_build_discovery_factory_picks_right_class() -> None:
    assert isinstance(
        build_discovery({"mode": "explicit_ids", "ids": ["1"]}),
        ExplicitIdsDiscovery,
    )
    assert isinstance(
        build_discovery({"mode": "bip_report", "report_path": "/x"}),
        BipReportDiscovery,
    )
    with pytest.raises(ConfigError, match="Unknown discovery.mode"):
        build_discovery({"mode": "garbage"})


# ============================================================================
# get_mass_addition (processTransaction-getMassAddition)
# ============================================================================
def test_get_mass_addition_posts_processTransaction(monkeypatch) -> None:
    client = _client(monkeypatch)

    response_body = {
        "OperationName": "processTransaction-getMassAddition",
        "ParameterList": (
            '{"X_RETURN_STATUS":"S",'
            '"X_BOOK_TYPE_CODE":"US CORP BOOK",'
            '"X_TAG_NUMBER":"TAG-100",'
            '"X_SERIAL_NUMBER":"SN-100",'
            '"X_PO_NUMBER":"PO-100",'
            '"X_INVOICE_NUMBER":"INV-100",'
            '"X_COST":"15000",'
            '"X_UNITS":"3",'
            '"X_PAYABLES_UNITS":"3",'
            '"X_QUEUE_NAME":"NEW",'
            '"X_POSTING_STATUS":"NEW"}'
        ),
    }
    fake_post = MagicMock(return_value=_ok(response_body))
    with patch.object(client._session, "post", fake_post):
        ma = client.get_mass_addition("300100180277508")

    # Verify we POSTed processTransaction-getMassAddition with the right ID.
    body = fake_post.call_args.kwargs["json"]
    assert body["OperationName"] == "processTransaction-getMassAddition"
    assert "P_MASS_ADDITION_ID:" in body["ParameterList"]
    assert "300100180277508" in body["ParameterList"]

    # And we mapped X_* fields into MassAddition correctly.
    assert ma.mass_addition_id == "300100180277508"
    assert ma.book_type_code == "US CORP BOOK"
    assert ma.tag_number == "TAG-100"
    assert ma.serial_number == "SN-100"
    assert ma.po_number == "PO-100"
    assert ma.invoice_number == "INV-100"
    assert ma.cost == 15000.0
    assert ma.invoice_quantity == 3
    assert ma.received_quantity == 3
    assert ma.queue_name == "NEW"
    assert ma.posting_status == "NEW"


def test_get_mass_addition_raises_on_error_status(monkeypatch) -> None:
    client = _client(monkeypatch)
    body = {
        "ParameterList": (
            '{"X_RETURN_STATUS":"E","X_MSG_DATA":"unknown mass addition id"}'
        )
    }
    with patch.object(client._session, "post", return_value=_ok(body)):
        with pytest.raises(FusionApiError, match="X_RETURN_STATUS='E'"):
            client.get_mass_addition("does-not-exist")


def test_get_mass_addition_handles_loose_oracle_format(monkeypatch) -> None:
    """Some handles return ParameterList in Oracle's PL-format, not strict JSON."""
    client = _client(monkeypatch)
    body = {
        "ParameterList": (
            "{X_RETURN_STATUS:'S',X_BOOK_TYPE_CODE:'US CORP BOOK',"
            "X_TAG_NUMBER:'TAG-7',X_COST:1234.5}"
        )
    }
    with patch.object(client._session, "post", return_value=_ok(body)):
        ma = client.get_mass_addition("7")
    assert ma.tag_number == "TAG-7"
    assert ma.cost == 1234.5


def test_get_mass_addition_raises_on_http_error(monkeypatch) -> None:
    client = _client(monkeypatch)
    with patch.object(client._session, "post", return_value=_err(500, "broke")):
        with pytest.raises(FusionApiError, match="HTTP 500"):
            client.get_mass_addition("1")


# ============================================================================
# update_mass_addition (processTransaction-updateMassAddition)
# ============================================================================
def test_update_mass_addition_posts_processTransaction(monkeypatch) -> None:
    client = _client(monkeypatch)

    success_body = {
        "OperationName": "processTransaction-updateMassAddition",
        "ParameterList": '{"X_MSG_COUNT":"0","X_RETURN_STATUS":"S"}',
    }
    fake_post = MagicMock(return_value=_ok(success_body))

    payload = {
        "P_MASS_ADDITION_ID": "MA-100",
        "P_BOOK_TYPE_CODE": "US CORP BOOK",
        "P_QUEUE_NAME": "POST",
        "P_POSTING_STATUS": "POST",
        "P_TAG_NUMBER": "TAG-100",
        "P_PAYABLES_CCID": 12001,
    }

    with patch.object(client._session, "post", fake_post):
        result = client.update_mass_addition(payload)

    assert "X_RETURN_STATUS" in result
    body = fake_post.call_args.kwargs["json"]
    assert body["OperationName"] == "processTransaction-updateMassAddition"
    assert "P_MASS_ADDITION_ID:'MA-100'" in body["ParameterList"]
    assert "P_TAG_NUMBER:'TAG-100'" in body["ParameterList"]
    assert "P_PAYABLES_CCID: 12001" in body["ParameterList"]


def test_update_mass_addition_raises_on_http_error(monkeypatch) -> None:
    client = _client(monkeypatch)
    with patch.object(client._session, "post", return_value=_err(401, "unauthorized")):
        with pytest.raises(FusionApiError, match="HTTP 401"):
            client.update_mass_addition({"P_MASS_ADDITION_ID": "1"})


def test_update_mass_addition_treats_request_exception_as_error(monkeypatch) -> None:
    client = _client(monkeypatch)
    with patch.object(
        client._session, "post", side_effect=requests_exception()
    ):
        with pytest.raises(FusionApiError, match="failed"):
            client.update_mass_addition({"P_MASS_ADDITION_ID": "1"})


def requests_exception():
    import requests

    return requests.ConnectionError("network down")


# ============================================================================
# End-to-end (drops into MassAdditionCycleRunner)
# ============================================================================
def test_fusion_client_drops_into_cycle_runner(monkeypatch, tmp_path) -> None:
    """Both list and get round-trip through the runner without modification."""
    from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner

    monkeypatch.setenv("FUSION_JWT", "tok")

    discovery = ExplicitIdsDiscovery(ids=("300100180277508",))
    client = FusionFaClient(_config(), discovery=discovery)

    get_response = _ok(
        {
            "ParameterList": (
                '{"X_RETURN_STATUS":"S",'
                '"X_BOOK_TYPE_CODE":"CORP_BOOK",'
                '"X_REGION":"US",'
                '"X_TAG_NUMBER":"TAG-100",'
                '"X_COST":"5000",'
                '"X_UNITS":"1",'
                '"X_PAYABLES_UNITS":"1"}'
            )
        }
    )

    runner = MassAdditionCycleRunner(
        oracle_client=client,
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
    )

    with patch.object(client._session, "post", return_value=get_response):
        result = runner.run(output_dir=tmp_path)

    # CMDB lookup runs (mock); TAG-100 exists in MOCK_CMDB so this enriches.
    assert result["total"] == 1
    assert result["auto_update"] == 1
    assert result["exception"] == 0


# ============================================================================
# _parse_parameter_list
# ============================================================================
def test_parse_pl_strict_json() -> None:
    out = _parse_parameter_list('{"X_RETURN_STATUS":"S","X_COST":"100"}')
    assert out == {"X_RETURN_STATUS": "S", "X_COST": "100"}


def test_parse_pl_oracle_loose_format_with_quotes() -> None:
    out = _parse_parameter_list("{X_RETURN_STATUS:'S',X_TAG_NUMBER:'TAG-1'}")
    assert out["X_RETURN_STATUS"] == "S"
    assert out["X_TAG_NUMBER"] == "TAG-1"


def test_parse_pl_oracle_loose_format_with_numbers() -> None:
    out = _parse_parameter_list("{X_COST:1500.5,X_UNITS:3}")
    assert out["X_COST"] == "1500.5"
    assert out["X_UNITS"] == "3"


def test_parse_pl_handles_dict_input() -> None:
    assert _parse_parameter_list({"X_FOO": "bar"}) == {"X_FOO": "bar"}


def test_parse_pl_handles_none_and_empty() -> None:
    assert _parse_parameter_list(None) == {}
    assert _parse_parameter_list("") == {}
    assert _parse_parameter_list("   ") == {}
