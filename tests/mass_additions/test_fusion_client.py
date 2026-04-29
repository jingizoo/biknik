"""Tests for the real Fusion FA mass-additions client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.exceptions import FusionApiError
from ofam_mass_additions.oracle.fusion_client import (
    FusionFaClient,
    FusionFaConfig,
)


def _ok(body: dict) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _err(status: int, text: str = "boom") -> MagicMock:
    resp = MagicMock(status_code=status, text=text)
    return resp


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


# ----- from_dict ----------------------------------------------------------


def test_from_dict_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        FusionFaConfig.from_dict({})


def test_from_dict_strips_trailing_slash_and_takes_overrides() -> None:
    cfg = FusionFaConfig.from_dict(
        {
            "base_url": "https://fa.example.com/",
            "api_version": "11.13.18.05",
            "tag_number_attribute": "Attribute2",
            "page_size": 100,
        }
    )
    assert cfg.base_url == "https://fa.example.com"
    assert cfg.tag_number_attribute == "Attribute2"
    assert cfg.page_size == 100


# ----- auth ---------------------------------------------------------------


def test_missing_bearer_raises(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_JWT", raising=False)
    client = FusionFaClient(_config())
    with pytest.raises(FusionApiError, match="No Fusion JWT available"):
        client._auth_headers()


def test_bearer_header_is_built(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())
    assert client._auth_headers()["Authorization"] == "Bearer tok"


# ----- list_new_mass_addition_ids -----------------------------------------


def test_list_new_walks_pages_until_no_more(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config(page_size=2))

    page1 = _ok(
        {
            "items": [
                {"MassAdditionId": "1"},
                {"MassAdditionId": "2"},
            ],
            "hasMore": True,
        }
    )
    page2 = _ok(
        {
            "items": [{"MassAdditionId": "3"}],
            "hasMore": False,
        }
    )
    with patch.object(client._session, "get", side_effect=[page1, page2]) as get:
        ids = client.list_new_mass_addition_ids(book_type_code="CORP_BOOK")

    assert ids == ["1", "2", "3"]
    assert get.call_count == 2

    # Sanity-check the query shape.
    first_params = get.call_args_list[0].kwargs["params"]
    assert first_params["q"] == "PostingStatus='NEW' and BookTypeCode='CORP_BOOK'"
    assert first_params["limit"] == "2"
    assert first_params["offset"] == "0"
    assert get.call_args_list[1].kwargs["params"]["offset"] == "2"


def test_list_new_returns_empty_on_no_results(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())
    with patch.object(client._session, "get", return_value=_ok({"items": [], "hasMore": False})):
        assert client.list_new_mass_addition_ids(book_type_code="X") == []


def test_list_new_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())
    with patch.object(client._session, "get", return_value=_err(500, "broke")):
        with pytest.raises(FusionApiError, match="500"):
            client.list_new_mass_addition_ids(book_type_code="X")


# ----- get_mass_addition --------------------------------------------------


def test_get_mass_addition_maps_dff_attributes(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())

    body = {
        "MassAdditionId": 42,
        "BookTypeCode": "CORP_BOOK",
        "Country": "US",
        "QueueName": "NEW",
        "PostingStatus": "NEW",
        "Attribute1": "TAG-XYZ",
        "Attribute5": "SN-XYZ",
        "PoNumber": "PO-1",
        "InvoiceNumber": "INV-1",
        "InvoiceQuantity": 3,
        "ReceivedQuantity": 3,
        "Cost": "1234.56",
    }
    with patch.object(client._session, "get", return_value=_ok(body)):
        ma = client.get_mass_addition("42")

    assert ma.mass_addition_id == "42"
    assert ma.book_type_code == "CORP_BOOK"
    assert ma.region == "US"
    assert ma.tag_number == "TAG-XYZ"
    assert ma.serial_number == "SN-XYZ"
    assert ma.invoice_quantity == 3
    assert ma.received_quantity == 3
    assert ma.cost == 1234.56


def test_get_mass_addition_handles_custom_dff_slots(monkeypatch) -> None:
    """Tenant override: tag in Attribute2, serial in Attribute7."""
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(
        _config(tag_number_attribute="Attribute2", serial_number_attribute="Attribute7")
    )
    body = {
        "MassAdditionId": "1",
        "BookTypeCode": "CORP_BOOK",
        "Attribute2": "TAG-CUSTOM",
        "Attribute7": "SN-CUSTOM",
    }
    with patch.object(client._session, "get", return_value=_ok(body)):
        ma = client.get_mass_addition("1")
    assert ma.tag_number == "TAG-CUSTOM"
    assert ma.serial_number == "SN-CUSTOM"


# ----- update_mass_addition -----------------------------------------------


def test_update_mass_addition_posts_processTransaction(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())

    success_body = {
        "OperationName": "processTransaction-updateMassAddition",
        "ParameterList": "{X_RETURN_STATUS:'S',X_MSG_DATA:'updated'}",
    }
    fake_post = MagicMock(return_value=_ok(success_body))
    payload = {
        "P_MASS_ADDITION_ID": "MA-100",
        "P_BOOK_TYPE_CODE": "CORP_BOOK",
        "P_QUEUE_NAME": "POST",
        "P_POSTING_STATUS": "POST",
        "P_ASSET_TYPE": "CAPITALIZED",
        "P_LOCATION_ID_TBL": [501],
        "P_DEPRN_EXPENSE_CCID_TBL": [12001],
        "P_EMPLOYEE_ID_TBL": [8801],
    }

    with patch.object(client._session, "post", fake_post):
        result = client.update_mass_addition(payload)

    assert "X_RETURN_STATUS" in result
    body_sent = fake_post.call_args.kwargs["json"]
    assert body_sent["OperationName"] == "processTransaction-updateMassAddition"
    # ParameterList is a string in Oracle's expected format.
    assert body_sent["ParameterList"].startswith("{")
    assert "P_MASS_ADDITION_ID:'MA-100'" in body_sent["ParameterList"]
    assert "P_ASSET_TYPE:'CAPITALIZED'" in body_sent["ParameterList"]
    # Rosetta tables are single-quoted comma lists.
    assert "P_LOCATION_ID_TBL:'501'" in body_sent["ParameterList"]


def test_update_mass_addition_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())
    with patch.object(client._session, "post", return_value=_err(401, "unauthorized")):
        with pytest.raises(FusionApiError, match="401"):
            client.update_mass_addition({"P_MASS_ADDITION_ID": "1"})


# ----- end-to-end smoke through the cycle runner --------------------------


def test_fusion_client_drops_into_cycle_runner(monkeypatch, tmp_path) -> None:
    """The new FusionFaClient must satisfy the same protocol as the mock."""
    from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner

    monkeypatch.setenv("FUSION_JWT", "tok")
    client = FusionFaClient(_config())

    list_resp = _ok({"items": [{"MassAdditionId": "1"}], "hasMore": False})
    get_resp = _ok(
        {
            "MassAdditionId": "1",
            "BookTypeCode": "CORP_BOOK",
            "Country": "US",
            "PostingStatus": "NEW",
            "Attribute1": "TAG-100",  # matches mock CMDB
            "InvoiceQuantity": 1,
            "ReceivedQuantity": 1,
            "Cost": "5000.0",
        }
    )

    runner = MassAdditionCycleRunner(
        oracle_client=client,
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
    )

    with patch.object(client._session, "get", side_effect=[list_resp, get_resp]):
        result = runner.run(output_dir=tmp_path)

    # CMDB lookup runs (mock); TAG-100 hits, so this row enriches.
    assert result["total"] == 1
    assert result["auto_update"] == 1
    assert result["exception"] == 0
