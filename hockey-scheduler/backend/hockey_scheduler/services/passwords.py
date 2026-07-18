"""Password hashing for real user accounts (#67).

Stdlib-only (``hashlib``/``secrets``/``hmac``) so the suite keeps running
anywhere with no third-party dependency. PBKDF2-HMAC-SHA256 with a per-password
random salt; the iteration count is embedded in the stored string so it can be
raised later without invalidating existing hashes.
"""

import hashlib
import hmac
import os
import secrets
from typing import Optional

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 260_000
_SALT_BYTES = 16

# -- minimum credential policy (#267) --------------------------------------
#
# A newly created / activated / reset / changed password must meet a minimum
# length in PRODUCTION. Development/demo credentials (e.g. the seeded "demo"
# password) stay explicitly development-only: the policy is enforced only when
# APP_MODE=production, and the demo seed paths run only outside production. The
# minimum is configurable via HS_MIN_PASSWORD_LENGTH within safe bounds — in
# production it can be raised but never lowered below the floor, so a stray or
# accidental override can NEVER weaken the policy (mirrors the PBKDF2-iteration
# floor). Existing stored hashes are never invalidated: the policy applies at
# the next create/change/reset, not to already-stored credentials.
_MIN_PASSWORD_LENGTH_DEFAULT = 10
_MIN_PASSWORD_LENGTH_FLOOR = 8
# An upper bound so a misconfigured value can't wedge account creation (no
# reachable password could satisfy an absurd minimum) — safe bounds in both
# directions, mirroring the login-throttle clamps.
_MIN_PASSWORD_LENGTH_CEILING = 128


def _is_production() -> bool:
    return (os.environ.get("APP_MODE") or "demo").strip().lower() == "production"


def min_password_length() -> int:
    """The enforced minimum new-password length. Read at call time so a
    deployment can configure it; clamped to a safe ``[floor, ceiling]`` range in
    production so a stray override can neither weaken the policy below the floor
    nor raise it to an unreachable value."""
    raw = os.environ.get("HS_MIN_PASSWORD_LENGTH")
    if not raw:
        return _MIN_PASSWORD_LENGTH_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _MIN_PASSWORD_LENGTH_DEFAULT
    if _is_production():
        return min(_MIN_PASSWORD_LENGTH_CEILING, max(n, _MIN_PASSWORD_LENGTH_FLOOR))
    return min(_MIN_PASSWORD_LENGTH_CEILING, max(1, n))


def password_policy_error(password) -> Optional[str]:
    """Return a stable, user-facing reason string if ``password`` violates the
    minimum credential policy, else ``None``.

    Always requires a non-empty string. In production it additionally requires
    at least ``min_password_length()`` characters; outside production only the
    non-empty rule applies, so development/demo credentials remain usable while
    production account paths cannot be created below the configured minimum.
    """
    if not isinstance(password, str) or not password:
        return "A password is required."
    if _is_production() and len(password) < min_password_length():
        return (f"Password must be at least {min_password_length()} "
                "characters.")
    return None


def _resolve_iterations() -> int:
    """Resolve the PBKDF2 cost once, at import.

    ``HS_PBKDF2_ITERATIONS`` is a TEST-only knob: the test bootstrap
    (tests/helpers.py) lowers it so the suite's hundreds of ~80 ms hash/verify
    calls don't dominate runtime. It is **ignored whenever APP_MODE=production**,
    so a stray or accidental override in a production environment can NEVER
    weaken hashing below the strong default (#266 review) — production always
    gets ``_DEFAULT_ITERATIONS``. Safe to lower elsewhere because the iteration
    count is embedded in every stored hash (so low-cost test hashes and strong
    production hashes both verify), and the DUMMY placeholder pays the same cost
    as a real hash either way, preserving the timing-oracle defence.
    """
    production = (os.environ.get("APP_MODE") or "demo").strip().lower() == "production"
    override = os.environ.get("HS_PBKDF2_ITERATIONS")
    if override and not production:
        try:
            return max(1, int(override))
        except ValueError:
            return _DEFAULT_ITERATIONS
    return _DEFAULT_ITERATIONS


_ITERATIONS = _resolve_iterations()


def hash_password(password: str) -> str:
    """Return an opaque, storable hash: ``algorithm$iterations$salt$hash``."""
    salt = secrets.token_hex(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a hash from ``hash_password``.

    Returns False (never raises) for a malformed/foreign stored value, so a
    corrupted record fails closed instead of crashing login.
    """
    try:
        algorithm, iterations, salt, expected_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt),
            int(iterations))
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (ValueError, AttributeError):
        return False


# A real-cost placeholder hash for the "unknown username" login path, so that
# case pays the same PBKDF2 work as a known username with a wrong password —
# otherwise the response-time difference would leak whether an account exists.
DUMMY_PASSWORD_HASH = hash_password("__dummy_password_never_valid__")
