from datetime import datetime

import pytest

from ofam_mass_additions.oracle.payloads import (
    build_operation_payload,
    parse_oracle_status,
    serialize_parameter_list,
    validate_uppercase_params,
)


def test_validate_uppercase_params_rejects_lowercase() -> None:
    with pytest.raises(ValueError):
        validate_uppercase_params({"p_mass_addition_id": "MA-1"})


def test_build_operation_payload_uses_expected_operation_name() -> None:
    payload = build_operation_payload("getMassAddition", {"P_MASS_ADDITION_ID": "MA-100"})
    assert payload["OperationName"] == "processTransaction-getMassAddition"


def test_parse_oracle_status_reads_return_status() -> None:
    status, msg = parse_oracle_status('{"X_RETURN_STATUS":"S","X_MSG_DATA":"ok"}')
    assert status == "S"
    assert msg == "ok"


def test_serialize_parameter_list_preserves_yyyy_mm_dd_dates() -> None:
    value = serialize_parameter_list({"P_DATE_PLACED_IN_SERVICE": datetime(2026, 1, 15)})
    assert "2026-01-15" in value
