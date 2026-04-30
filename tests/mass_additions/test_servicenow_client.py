"""Tests for the ServiceNow CMDB client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.cmdb.servicenow_client import (
    ServiceNowCmdbClient,
    ServiceNowConfig,
    make_servicenow_lookup,
)
from ofam_mass_additions.models import MassAddition


def _ok_response(rows: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"result": rows}
    resp.raise_for_status.return_value = None
    return resp


def _config() -> ServiceNowConfig:
    return ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
    )


# ----- ServiceNowConfig.from_dict -----------------------------------------


def test_from_dict_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        ServiceNowConfig.from_dict({})


def test_from_dict_overrides_field_names() -> None:
    cfg = ServiceNowConfig.from_dict(
        {
            "base_url": "https://snow.example.com",
            "bearer_token_env": "SNOW_TOKEN",
            "expense_ccid_field": "u_fa_expense_ccid",
            "ccid_active_field": "u_ccid_active",
            "extra_fields": ["u_custom"],
        }
    )
    assert cfg.expense_ccid_field == "u_fa_expense_ccid"
    assert cfg.ccid_active_field == "u_ccid_active"
    assert cfg.extra_fields == ("u_custom",)


# ----- Auth header precedence ---------------------------------------------


def test_bearer_takes_precedence_over_api_key(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "bearer-xyz")
    monkeypatch.setenv("SNOW_KEY", "basic-abc")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        api_key_env="SNOW_KEY",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    assert headers["Authorization"] == "Bearer bearer-xyz"


def test_basic_auth_used_when_no_bearer(monkeypatch) -> None:
    monkeypatch.delenv("SNOW_TOKEN", raising=False)
    monkeypatch.setenv("SNOW_KEY", "basic-abc")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        api_key_env="SNOW_KEY",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    assert headers["Authorization"] == "Basic basic-abc"


def test_no_auth_configured_raises(monkeypatch) -> None:
    monkeypatch.delenv("SNOW_TOKEN", raising=False)
    cfg = ServiceNowConfig(base_url="https://snow.example.com")
    with pytest.raises(RuntimeError, match="auth not configured"):
        ServiceNowCmdbClient(cfg)._auth_headers()


def test_username_password_builds_basic_header(monkeypatch) -> None:
    """Raw username + password env var -> base64 Basic header (Postman-style)."""
    import base64

    monkeypatch.delenv("SNOW_TOKEN", raising=False)
    monkeypatch.setenv("SNOW_PW", "p@ss:w0rd")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        username="sp_orc_fdi",
        password_env="SNOW_PW",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    expected = base64.b64encode(b"sp_orc_fdi:p@ss:w0rd").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"


def test_bearer_still_wins_over_username_password(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "bearer-xyz")
    monkeypatch.setenv("SNOW_PW", "secret")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        username="sp_orc_fdi",
        password_env="SNOW_PW",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    assert headers["Authorization"] == "Bearer bearer-xyz"


def test_username_password_wins_over_pre_encoded_api_key(monkeypatch) -> None:
    """Pre-encoded api_key was the legacy path; raw user/pass is friendlier."""
    import base64

    monkeypatch.delenv("SNOW_TOKEN", raising=False)
    monkeypatch.setenv("SNOW_PW", "secret")
    monkeypatch.setenv("SNOW_KEY", "legacy-pre-encoded")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        username="sp_orc_fdi",
        password_env="SNOW_PW",
        api_key_env="SNOW_KEY",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    expected = base64.b64encode(b"sp_orc_fdi:secret").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"


def test_username_without_password_env_falls_through(monkeypatch) -> None:
    """If password env var is unset, fall through to api_key path."""
    monkeypatch.delenv("SNOW_TOKEN", raising=False)
    monkeypatch.delenv("SNOW_PW", raising=False)
    monkeypatch.setenv("SNOW_KEY", "fallback-encoded")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        username="sp_orc_fdi",
        password_env="SNOW_PW",
        api_key_env="SNOW_KEY",
    )
    headers = ServiceNowCmdbClient(cfg)._auth_headers()
    assert headers["Authorization"] == "Basic fallback-encoded"


def test_from_dict_reads_username_password() -> None:
    cfg = ServiceNowConfig.from_dict(
        {
            "base_url": "https://snow.example.com",
            "username": "sp_orc_fdi",
            "password_env": "SNOW_PW",
        }
    )
    assert cfg.username == "sp_orc_fdi"
    assert cfg.password_env == "SNOW_PW"


# ----- lookup() happy path -------------------------------------------------


def test_lookup_by_tag_returns_cmdb_asset(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    cfg = _config()
    client = ServiceNowCmdbClient(cfg)

    row = {
        "sys_id": "snow-sysid-1",
        "asset_tag": "TAG-100",
        "serial_number": "SN-100",
        "location.u_fin_location_id": "501",
        "cost_center.value": "12001",
        "assigned_to.u_employee_id": "8801",
        "model_category.name": "Laptop",
    }
    fake_get = MagicMock(return_value=_ok_response([row]))
    with patch.object(client._session, "get", fake_get):
        hit = client.lookup(field_name="tag_number", value="TAG-100")

    assert hit is not None
    assert hit.source_key == "tag_number"
    assert hit.source_value == "TAG-100"
    assert hit.location_id == 501
    assert hit.expense_ccid == 12001
    assert hit.employee_id == 8801
    assert hit.ccid_active is True

    # Verify the actual HTTP request shape.
    call_kwargs = fake_get.call_args.kwargs
    assert call_kwargs["params"]["sysparm_query"] == "asset_tag=TAG-100"
    assert call_kwargs["params"]["sysparm_limit"] == "1"
    assert call_kwargs["params"]["sysparm_exclude_reference_link"] == "true"
    assert "asset_tag" in call_kwargs["params"]["sysparm_fields"]
    assert call_kwargs["headers"]["Authorization"] == "Bearer tok"


def test_lookup_by_serial_number(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    row = {
        "sys_id": "x",
        "serial_number": "SN-9",
        "location.u_fin_location_id": "10",
        "cost_center.value": "20",
        "assigned_to.u_employee_id": "30",
    }
    with patch.object(client._session, "get", return_value=_ok_response([row])) as get:
        hit = client.lookup(field_name="serial_number", value="SN-9")

    assert hit is not None
    assert hit.source_key == "serial_number"
    assert get.call_args.kwargs["params"]["sysparm_query"] == "serial_number=SN-9"


def test_lookup_returns_none_when_no_results(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    with patch.object(client._session, "get", return_value=_ok_response([])):
        assert client.lookup(field_name="tag_number", value="MISSING") is None


def test_lookup_returns_none_when_field_unsupported(monkeypatch) -> None:
    """po_number / invoice_number aren't on alm_hardware by default."""
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    fake_get = MagicMock()
    with patch.object(client._session, "get", fake_get):
        assert client.lookup(field_name="po_number", value="PO-1") is None

    fake_get.assert_not_called()


def test_lookup_handles_404_as_miss(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    resp = MagicMock(status_code=404)
    with patch.object(client._session, "get", return_value=resp):
        assert client.lookup(field_name="tag_number", value="X") is None


def test_lookup_treats_request_exception_as_miss(monkeypatch) -> None:
    """Network failures shouldn't crash the cycle; the asset just won't enrich."""
    import requests

    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    with patch.object(
        client._session, "get", side_effect=requests.ConnectionError("boom")
    ):
        assert client.lookup(field_name="tag_number", value="X") is None


def test_lookup_parses_ccid_active_field_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    cfg = ServiceNowConfig(
        base_url="https://snow.example.com",
        bearer_token_env="SNOW_TOKEN",
        ccid_active_field="u_ccid_active",
    )
    client = ServiceNowCmdbClient(cfg)
    row = {
        "asset_tag": "T",
        "location.u_fin_location_id": "1",
        "cost_center.value": "2",
        "assigned_to.u_employee_id": "3",
        "u_ccid_active": "false",
    }
    with patch.object(client._session, "get", return_value=_ok_response([row])):
        hit = client.lookup(field_name="tag_number", value="T")
    assert hit is not None
    assert hit.ccid_active is False


def test_lookup_returns_none_int_on_non_numeric_field(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    client = ServiceNowCmdbClient(_config())
    row = {
        "asset_tag": "T",
        "location.u_fin_location_id": "",
        "cost_center.value": "ENG-NA",  # non-numeric label
        "assigned_to.u_employee_id": None,
    }
    with patch.object(client._session, "get", return_value=_ok_response([row])):
        hit = client.lookup(field_name="tag_number", value="T")
    assert hit is not None
    assert hit.location_id is None
    assert hit.expense_ccid is None
    assert hit.employee_id is None


# ----- make_servicenow_lookup walks LOOKUP_ORDER --------------------------


def test_make_servicenow_lookup_falls_through_to_serial_when_tag_missing(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    cfg = _config()
    lookup = make_servicenow_lookup(cfg)
    ma = MassAddition(
        mass_addition_id="MA1",
        book_type_code="CORP",
        region="US",
        tag_number=None,
        serial_number="SN-9",
    )
    row = {
        "serial_number": "SN-9",
        "location.u_fin_location_id": "1",
        "cost_center.value": "2",
        "assigned_to.u_employee_id": "3",
    }
    # Patch on the Session class so make_servicenow_lookup's internal
    # client picks it up.
    with patch("requests.Session.get", return_value=_ok_response([row])) as get:
        hit = lookup(ma)

    assert hit is not None
    assert hit.source_key == "serial_number"
    assert get.call_args.kwargs["params"]["sysparm_query"] == "serial_number=SN-9"


def test_make_servicenow_lookup_returns_none_when_no_field_matches(monkeypatch) -> None:
    monkeypatch.setenv("SNOW_TOKEN", "tok")
    lookup = make_servicenow_lookup(_config())
    ma = MassAddition(mass_addition_id="MA1", book_type_code="CORP", region="US")
    # No tag/serial/po/invoice on the mass addition → no calls made.
    with patch("requests.Session.get") as get:
        assert lookup(ma) is None
    get.assert_not_called()
