"""First-admin bootstrap for production (#71).

Production mode (#68) starts with **zero** accounts and no self-service signup,
so there must be a safe, out-of-band way to create the very first operator.
This module provides exactly that — and nothing more (no public signup, no
password reset).

Two entry points, same core:
  - ``bootstrap_admin_from_env(api, env)`` — called at server start; reads
    ``BOOTSTRAP_ADMIN_USER`` / ``BOOTSTRAP_ADMIN_PASSWORD`` and creates the
    first league admin if none exists.
  - ``python -m hockey_scheduler.bootstrap`` — a one-off CLI for a persistent
    (``DATABASE_URL``) store, taking the same values via flags or env.

The operation is **first-admin only**: it creates the account solely when the
store has no accounts yet, so re-running it (every boot, or by hand) never
clobbers or duplicates an existing operator.
"""

import argparse
import os
import sys

from .domain import Role


def bootstrap_admin(api, username: str, password: str):
    """Create the first league-admin account if the store has none.

    Returns ``(created: bool, message: str)``. Never raises for the "already
    bootstrapped" case — it is a safe no-op so this can run on every boot.
    """
    username = (username or "").strip().lower()
    if not username or not password:
        return False, "BOOTSTRAP_ADMIN_USER and BOOTSTRAP_ADMIN_PASSWORD are both required."
    existing = api.accounts.list_accounts()
    if existing:
        return False, f"Skipped: {len(existing)} account(s) already exist."
    api.accounts.create_account(
        username, password, Role.LEAGUE_ADMIN, actor_id="bootstrap")
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
