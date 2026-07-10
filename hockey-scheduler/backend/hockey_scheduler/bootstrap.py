"""First-admin bootstrap and one-time browser installation claim (#71/#174).

Production starts with zero accounts and no public signup. Operations can still
bootstrap the first admin through environment variables or the CLI, while a
fresh durable client deployment can instead be claimed in the browser with a
separate high-entropy ``INITIAL_SETUP_CODE``. Both paths share the same atomic
first-admin service, so they cannot race or create two initial administrators.
"""

import argparse
import hashlib
import hmac
import os
import sys

from .domain.errors import (
    AlreadyClaimedError,
    ClaimUnavailableError,
    InvalidSetupCodeError,
)

INITIAL_SETUP_CODE = "INITIAL_SETUP_CODE"


def _store_is_durable(store) -> bool:
    """Whether ``store`` is a real Postgres or file-backed SQLite database."""
    return (getattr(store, "backend", None) in ("postgres", "sqlite")
            and not bool(getattr(store, "is_memory_backed", True)))


def installation_claim_status(api, env, app_mode: str) -> dict:
    """Return the minimal public-safe availability state for ``/setup``.

    The response deliberately contains no account count, username, database URL,
    backend name, or configuration value. ``reason`` is a stable coarse category
    the setup page can turn into either a sign-in link or an operations message.
    """
    if (app_mode or "").strip().lower() != "production":
        return {"claim_available": False, "reason": "not_production"}

    store = api.store
    if not _store_is_durable(store):
        return {"claim_available": False, "reason": "durable_store_required"}
    try:
        if not store.db_reachable():
            return {"claim_available": False, "reason": "deployment_not_ready"}
        if not store.migration_status().get("current"):
            return {"claim_available": False, "reason": "deployment_not_ready"}
        if (store.get_installation_state("primary") is not None
                or bool(store.all_user_accounts())):
            return {"claim_available": False, "reason": "already_claimed"}
    except Exception:
        return {"claim_available": False, "reason": "deployment_not_ready"}

    if not env.get(INITIAL_SETUP_CODE):
        return {"claim_available": False, "reason": "setup_code_missing"}
    return {"claim_available": True, "reason": None}


def claim_installation(api, env, app_mode: str, setup_code: str,
                       username: str, password: str):
    """Validate the one-time code and atomically create the first League Admin.

    The raw code is used only to compute fixed-length SHA-256 digests that are
    compared with ``hmac.compare_digest``. It is never returned, persisted,
    audited, or included in an exception/detail payload.
    """
    status = installation_claim_status(api, env, app_mode)
    if not status["claim_available"]:
        if status["reason"] == "already_claimed":
            raise AlreadyClaimedError(
                "This installation has already been claimed.")
        raise ClaimUnavailableError(
            "Installation claim is unavailable. Contact the deployment administrator.")

    configured = str(env.get(INITIAL_SETUP_CODE) or "").encode("utf-8")
    submitted = (setup_code if isinstance(setup_code, str) else "").encode("utf-8")
    configured_digest = hashlib.sha256(configured).digest()
    submitted_digest = hashlib.sha256(submitted).digest()
    if not hmac.compare_digest(configured_digest, submitted_digest):
        raise InvalidSetupCodeError(
            "Unable to claim the installation with those details.")

    return api.accounts.create_first_admin_if_unclaimed(
        username, password, claim_method="browser")


def bootstrap_admin(api, username: str, password: str):
    """Create the first league-admin account if the installation is unclaimed.

    Returns ``(created: bool, message: str)``. Never raises for the "already
    bootstrapped" case — it is a safe no-op so this can run on every boot.
    """
    username = (username or "").strip().lower()
    if not username or not password:
        return False, "BOOTSTRAP_ADMIN_USER and BOOTSTRAP_ADMIN_PASSWORD are both required."
    try:
        api.accounts.create_first_admin_if_unclaimed(
            username, password, actor_id="bootstrap", claim_method="operations")
    except AlreadyClaimedError:
        existing = api.accounts.list_accounts()
        return False, f"Skipped: {len(existing)} account(s) already exist."
    return True, f"Created first league admin '{username}'."


def bootstrap_admin_from_env(api, env) -> bool:
    """Bootstrap from ``BOOTSTRAP_ADMIN_USER`` / ``BOOTSTRAP_ADMIN_PASSWORD``.

    Returns True only if an account was actually created. Absent env vars are
    a silent no-op (nothing to do), not an error.
    """
    user = env.get("BOOTSTRAP_ADMIN_USER")
    password = env.get("BOOTSTRAP_ADMIN_PASSWORD")
    if not user or not password:
        return False
    created, _ = bootstrap_admin(api, user, password)
    return created


def main(argv=None) -> int:
    """CLI: create the first admin in the configured (DATABASE_URL) store."""
    parser = argparse.ArgumentParser(
        prog="python -m hockey_scheduler.bootstrap",
        description="Create the first league-admin account (production #71).")
    parser.add_argument("--username", default=os.environ.get("BOOTSTRAP_ADMIN_USER"))
    parser.add_argument("--password", default=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"))
    args = parser.parse_args(argv)

    # Import here so the CLI has no import cost for the common (server) path.
    from .api import ApiService
    from .store import create_store

    api = ApiService(create_store())
    created, message = bootstrap_admin(api, args.username or "", args.password or "")
    print(message)
    return 0 if created else 1


if __name__ == "__main__":
    sys.exit(main())
