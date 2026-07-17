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

_ALGORITHM = "pbkdf2_sha256"
# The strong production default. Never lowered in production — the override
# below is set only by the test bootstrap (tests/helpers.py) so the suite's
# hundreds of hash/verify calls don't dominate runtime. Safe because the
# iteration count is embedded in every stored hash, so a low-cost test hash and
# a strong production hash both verify correctly, and the DUMMY placeholder pays
# the same (whatever) cost as a real hash, preserving the timing-oracle defence.
_DEFAULT_ITERATIONS = 260_000
_ITERATIONS = max(1, int(os.environ.get("HS_PBKDF2_ITERATIONS") or _DEFAULT_ITERATIONS))
_SALT_BYTES = 16


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
