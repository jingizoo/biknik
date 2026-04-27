from __future__ import annotations

import json
from datetime import datetime

SUPPORTED_HANDLES = {"getMassAddition", "updateMassAddition"}


def validate_uppercase_params(params: dict[str, object]) -> None:
    bad_keys = [key for key in params if key.upper() != key]
    if bad_keys:
        raise ValueError(f"Oracle parameter names must be uppercase: {bad_keys}")


def serialize_parameter_list(params: dict[str, object]) -> str:
    validate_uppercase_params(params)
    return json.dumps(params, separators=(",", ":"), sort_keys=True, default=_json_default)


def parse_parameter_list(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("ParameterList must parse to a JSON object")
    return parsed


def build_operation_payload(handle: str, params: dict[str, object]) -> dict[str, str]:
    if handle not in SUPPORTED_HANDLES:
        raise ValueError(f"Unsupported handle: {handle}")
    return {
        "OperationName": f"processTransaction-{handle}",
        "ParameterList": serialize_parameter_list(params),
    }


def parse_oracle_status(parameter_list: str) -> tuple[str, str]:
    parsed = parse_parameter_list(parameter_list)
    status = str(parsed.get("X_RETURN_STATUS", ""))
    msg = str(parsed.get("X_MSG_DATA", ""))
    return status, msg


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    raise TypeError(f"Object of type {type(value)} is not JSON serializable")
