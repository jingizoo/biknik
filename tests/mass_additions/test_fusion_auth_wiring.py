"""Smoke tests for the fusion_auth → token_provider → FusionFaClient wiring.

The full keytab/PingFed flow lives in
``ofam_mass_additions.fusion_token_provider`` and is exercised separately
in the asset-xfer test suite (the file is a verbatim copy).  These tests
just confirm the *wiring* — that ``FusionFaClient`` calls the provided
callable per request and that the runner builds the provider from the
``fusion_auth`` config block.

Both reads and writes go through ``processTransaction`` POSTs on
``/erpintegrations``, so the mocks target ``Session.post``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ofam_mass_additions.exceptions import FusionApiError
from ofam_mass_additions.oracle.discovery import ExplicitIdsDiscovery
from ofam_mass_additions.oracle.fusion_client import (
    FusionFaClient,
    FusionFaConfig,
)


def _ok(body: dict) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _config() -> FusionFaConfig:
    return FusionFaConfig(base_url="https://fa.example.com")


def _discovery() -> ExplicitIdsDiscovery:
    return ExplicitIdsDiscovery(ids=("300100180277508",))


# A getMassAddition response that won't make the runner sad.
_GET_RESPONSE_BODY = {
    "ParameterList": (
        '{"X_RETURN_STATUS":"S",'
        '"X_BOOK_TYPE_CODE":"US CORP BOOK",'
        '"X_TAG_NUMBER":"TAG-1",'
        '"X_COST":"100"}'
    )
}


def test_token_provider_called_per_request_when_provided() -> None:
    calls = {"n": 0}

    def fake_provider() -> str:
        calls["n"] += 1
        return f"jwt-{calls['n']}"

    client = FusionFaClient(_config(), discovery=_discovery(), token_provider=fake_provider)

    with patch.object(
        client._session, "post", return_value=_ok(_GET_RESPONSE_BODY),
    ) as post:
        client.get_mass_addition("300100180277508")

    assert calls["n"] == 1
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer jwt-1"


def test_token_provider_takes_precedence_over_env(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_JWT", "from-env")
    client = FusionFaClient(
        _config(), discovery=_discovery(), token_provider=lambda: "from-provider"
    )

    with patch.object(
        client._session, "post", return_value=_ok(_GET_RESPONSE_BODY),
    ) as post:
        client.get_mass_addition("300100180277508")

    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer from-provider"


def test_no_provider_and_empty_env_raises_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_JWT", raising=False)
    client = FusionFaClient(_config(), discovery=_discovery())
    with pytest.raises(FusionApiError, match="fusion_auth"):
        client._auth_headers()


def test_run_mass_additions_builds_provider_from_static_block(monkeypatch) -> None:
    """run_mass_additions._build_oracle_client wires fusion_auth to the client."""
    import sys

    sys.path.insert(0, ".")
    sys.path.insert(0, "src/main/python")
    import run_mass_additions

    monkeypatch.setenv("MY_TOKEN", "static-jwt-from-env")

    oracle_block = {
        "mode": "fusion",
        "base_url": "https://fa.example.com",
        "api_version": "11.13.18.05",
        "verify_ssl": False,
        "discovery": {"mode": "explicit_ids", "ids": ["300100180277508"]},
    }
    fusion_auth = {"mode": "static", "token_env": "MY_TOKEN"}

    import logging

    log = logging.getLogger("test")
    client = run_mass_additions._build_oracle_client(
        oracle_block, fusion_auth, "/tmp/x.json", log
    )

    with patch.object(
        client._session, "post", return_value=_ok(_GET_RESPONSE_BODY),
    ) as post:
        client.get_mass_addition("300100180277508")

    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer static-jwt-from-env"
