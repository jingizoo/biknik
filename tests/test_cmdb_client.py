"""Unit tests for ofam_asset_xfer.cmdb_client (CMDB/ServiceNow API client).

All tests use mocked HTTP responses — no network calls.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from ofam_asset_xfer.cmdb_client import CMDBClient, CMDBConfig
from ofam_asset_xfer.exceptions import CMDBApiError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    defaults = {
        "base_url": "https://test.service-now.com",
        "username": "testuser",
        "password": "testpass",
        "table": "alm_hardware",
    }
    defaults.update(overrides)
    return CMDBConfig(**defaults)


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {"result": []}
    resp.text = json.dumps(json_data or {})
    return resp


SAMPLE_ASSET = {
    "sys_id": "abc123",
    "asset_tag": "TAG-001",
    "display_name": "Dell Server R740",
    "serial_number": "SN12345",
    "u_cit_location_code": "US-NYC-01",
    "install_location": "New York DC",
    "location": "US-NYC",
    "company": "Citadel",
    "assigned_to": "John Doe",
    "model_category": "Server",
    "substatus": "In use",
    "install_status": "1",
}

SAMPLE_ASSET_2 = {
    "sys_id": "def456",
    "asset_tag": "TAG-002",
    "display_name": "HP Laptop EliteBook",
    "serial_number": "SN67890",
    "u_cit_location_code": "UK-LON-01",
    "install_location": "London Office",
    "location": "UK-LON",
    "company": "Citadel",
    "assigned_to": "Jane Smith",
    "model_category": "Laptop",
    "substatus": "In use",
    "install_status": "1",
}


# ===================================================================
# CMDBConfig
# ===================================================================

class TestCMDBConfig:
    def test_from_dict_basic(self):
        cfg = CMDBConfig.from_dict({
            "base_url": "https://test.service-now.com/",
            "username": "user",
            "password": "pass",
        })
        assert cfg.base_url == "https://test.service-now.com"  # trailing slash stripped
        assert cfg.username == "user"
        assert cfg.password == "pass"
        assert cfg.table == "alm_hardware"

    def test_from_dict_env_vars(self):
        with patch.dict("os.environ", {"MY_CMDB_USER": "env_user", "MY_CMDB_PASS": "env_pass"}):
            cfg = CMDBConfig.from_dict({
                "base_url": "https://test.service-now.com",
                "username_env": "MY_CMDB_USER",
                "password_env": "MY_CMDB_PASS",
            })
        assert cfg.username == "env_user"
        assert cfg.password == "env_pass"

    def test_from_dict_missing_url_raises(self):
        with pytest.raises(ValueError, match="base_url"):
            CMDBConfig.from_dict({"username": "u", "password": "p"})

    def test_from_dict_missing_credentials_raises(self):
        with pytest.raises(ValueError, match="credentials"):
            CMDBConfig.from_dict({"base_url": "https://test.com"})

    def test_from_dict_custom_table(self):
        cfg = CMDBConfig.from_dict({
            "base_url": "https://test.com",
            "username": "u",
            "password": "p",
            "table": "cmdb_ci_hardware",
        })
        assert cfg.table == "cmdb_ci_hardware"


# ===================================================================
# CMDBClient.get_assets
# ===================================================================

class TestGetAssets:
    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_get_assets_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            json_data={"result": [SAMPLE_ASSET, SAMPLE_ASSET_2]}
        )

        client = CMDBClient(_make_config())
        assets = client.get_assets(limit=10)

        assert len(assets) == 2
        assert assets[0]["asset_tag"] == "TAG-001"
        assert assets[1]["asset_tag"] == "TAG-002"

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_get_assets_with_query(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            json_data={"result": [SAMPLE_ASSET]}
        )

        client = CMDBClient(_make_config())
        assets = client.get_assets(query="company=Citadel", limit=5, offset=10)

        assert len(assets) == 1
        call_kwargs = mock_session.get.call_args
        params = call_kwargs[1].get("params") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["params"]
        assert params["sysparm_query"] == "company=Citadel"
        assert params["sysparm_limit"] == 5
        assert params["sysparm_offset"] == 10

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_get_assets_empty_result(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(json_data={"result": []})

        client = CMDBClient(_make_config())
        assets = client.get_assets()

        assert assets == []

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_get_assets_http_error_raises(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            status_code=401, json_data={"error": {"message": "Unauthorized"}}
        )

        client = CMDBClient(_make_config())
        with pytest.raises(CMDBApiError, match="401"):
            client.get_assets()

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_get_assets_non_json_raises(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("No JSON")
        resp.text = "<html>error</html>"
        mock_session.get.return_value = resp

        client = CMDBClient(_make_config())
        with pytest.raises(CMDBApiError, match="Non-JSON"):
            client.get_assets()


# ===================================================================
# CMDBClient.get_asset_by_tag
# ===================================================================

class TestGetAssetByTag:
    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_found(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            json_data={"result": [SAMPLE_ASSET]}
        )

        client = CMDBClient(_make_config())
        asset = client.get_asset_by_tag("TAG-001")

        assert asset is not None
        assert asset["asset_tag"] == "TAG-001"

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_not_found(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(json_data={"result": []})

        client = CMDBClient(_make_config())
        asset = client.get_asset_by_tag("NONEXISTENT")

        assert asset is None


# ===================================================================
# CMDBClient.get_all_assets (pagination)
# ===================================================================

class TestGetAllAssets:
    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_single_page(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            json_data={"result": [SAMPLE_ASSET]}
        )

        client = CMDBClient(_make_config())
        assets = client.get_all_assets(batch_size=100)

        assert len(assets) == 1
        # Only one API call since result < batch_size
        assert mock_session.get.call_count == 1

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_multi_page(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        page1 = _mock_response(json_data={"result": [SAMPLE_ASSET, SAMPLE_ASSET_2]})
        page2 = _mock_response(json_data={"result": [SAMPLE_ASSET]})
        mock_session.get.side_effect = [page1, page2]

        client = CMDBClient(_make_config())
        assets = client.get_all_assets(batch_size=2)

        assert len(assets) == 3
        assert mock_session.get.call_count == 2

    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_max_records_limit(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        # Return full pages every time
        full_page = _mock_response(json_data={"result": [SAMPLE_ASSET] * 100})
        mock_session.get.return_value = full_page

        client = CMDBClient(_make_config())
        assets = client.get_all_assets(batch_size=100, max_records=250)

        # Should stop after 3 pages (0, 100, 200) since 300 > max_records=250
        assert mock_session.get.call_count == 3


# ===================================================================
# CMDBClient.get_assets_with_location
# ===================================================================

class TestGetAssetsWithLocation:
    @patch("ofam_asset_xfer.cmdb_client.requests.Session")
    def test_builds_correct_query(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = _mock_response(
            json_data={"result": [SAMPLE_ASSET]}
        )

        client = CMDBClient(_make_config())
        assets = client.get_assets_with_location(additional_query="company=Citadel")

        assert len(assets) == 1
        call_kwargs = mock_session.get.call_args
        params = call_kwargs[1].get("params") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["params"]
        query = params["sysparm_query"]
        assert "u_cit_location_codeISNOTEMPTY" in query
        assert "install_status=1" in query
        assert "company=Citadel" in query
