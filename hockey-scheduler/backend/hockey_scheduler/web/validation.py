"""Strict write-body validation for the HTTP layer (#271).

Pure, stdlib-only helpers shared by ``Handler`` so covered write routes reject a
malformed or unexpected request body with a stable structured error envelope
instead of silently coercing it. Two long-standing defects motivate this:
malformed JSON used to become ``{}`` (and reach business logic as an empty
body); an unknown write-body key used to vanish silently. Everything here
raises ``BodyError``, which carries a ready-to-send ``{"error": {...}}`` payload
plus the HTTP status the caller should send with it — so a route only has to
``check_body(...)`` and, on ``BodyError``, ``_send_json(exc.payload,
exc.status)``.
"""

import json


class BodyError(Exception):
    """A request-body fault carrying a full error envelope + HTTP status.

    ``payload`` is the ``{"error": {"code", "message", "details"}}`` dict the
    caller sends verbatim; ``status`` is the HTTP status code to send with it.
    """

    def __init__(self, code: str, message: str, status: int,
                 details: dict = None):
        super().__init__(message)
        err = {"code": code, "message": message}
        if details:
            err["details"] = details
        self.payload = {"error": err}
        self.status = status


def parse_json_object(raw: bytes) -> dict:
    """Parse a request body as a JSON object.

    An empty body (no bytes) is a valid empty ``{}`` — many routes take no body.
    Invalid JSON, or a valid JSON value that isn't an object (a top-level
    array/string/number/etc.), raises ``BodyError`` (``malformed_json``, 400)
    so it never reaches business logic as a silently-coerced ``{}``.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise BodyError(
            "malformed_json",
            "The request body is not valid JSON.",
            400, {"reason": "malformed_json"})
    if not isinstance(value, dict):
        raise BodyError(
            "malformed_json",
            "The request body must be a JSON object.",
            400, {"reason": "not_an_object"})
    return value


def check_body(body: dict, *, allowed, required=(), types: dict = None) -> dict:
    """Validate a parsed body dict against a strict per-route schema.

    Faults are checked in a fixed order and the FIRST one raises ``BodyError``
    (a ``validation_error``, 400):

    * unknown keys                 → ``reason="unknown_field"``, ``fields=[...]``
    * missing/empty required key    → ``reason="field_required"``, ``field``
    * present key of the wrong type → ``reason="wrong_type"``, ``field``

    ``types`` maps a field to a type (or tuple of types) checked via
    ``isinstance`` for keys that are present and not ``None``. Returns ``body``
    so it can be used inline.
    """
    allowed = set(allowed)
    unknown = sorted(k for k in body if k not in allowed)
    if unknown:
        raise BodyError(
            "validation_error",
            "Unknown field(s): " + ", ".join(unknown) + ".",
            400, {"reason": "unknown_field", "fields": unknown})
    for field in required:
        value = body.get(field)
        if value is None or value == "":
            raise BodyError(
                "validation_error",
                f"The field '{field}' is required.",
                400, {"reason": "field_required", "field": field})
    for field, expected in (types or {}).items():
        value = body.get(field)
        if value is None:
            continue
        # ``bool`` is a subclass of ``int``, so ``isinstance(True, int)`` is
        # True — an int-expected field must still reject a JSON boolean (e.g. a
        # jersey_number of ``true``). Reject when the value is a bool but bool
        # isn't among the expected types even though int is.
        expected_types = expected if isinstance(expected, tuple) else (expected,)
        if isinstance(value, bool) and bool not in expected_types \
                and int in expected_types:
            raise BodyError(
                "validation_error",
                f"The field '{field}' has the wrong type.",
                400, {"reason": "wrong_type", "field": field})
        if not isinstance(value, expected):
            raise BodyError(
                "validation_error",
                f"The field '{field}' has the wrong type.",
                400, {"reason": "wrong_type", "field": field})
    return body
