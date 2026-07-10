"""One-shot deterministic source wiring for #174 PR B. Removed before merge."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path, old, new, count=1):
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(
            f"{path}: expected {count} occurrence(s), found {actual}: {old!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def append_once(path, marker, content):
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if marker in text:
        return
    target.write_text(text.rstrip() + "\n\n" + content.strip() + "\n",
                      encoding="utf-8")


# -- domain model ---------------------------------------------------------
replace(
    "backend/hockey_scheduler/domain/setup_models.py",
    "@dataclass\nclass UserAccount:\n",
    """@dataclass
class InstallationState:
    \"\"\"Durable one-time installation-claim marker (#174).

    The row contains only operational metadata. The setup code and the client's
    password are never persisted. A single primary-key id (``primary``) is the
    cross-process concurrency guard for first-admin creation.
    \"\"\"
    id: str
    claimed_at: datetime
    claimed_by_user_id: str
    claim_method: str


@dataclass
class UserAccount:
""",
)

# -- in-memory store ------------------------------------------------------
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "from contextlib import contextmanager\nfrom datetime import datetime\n",
    "from contextlib import contextmanager\nfrom datetime import datetime\nimport threading\n",
)
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "    GuardianLink,\n    NotificationEvent,\n",
    "    GuardianLink,\n    InstallationState,\n    NotificationEvent,\n",
)
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "        self.official_availability: Dict[str, OfficialAvailability] = {}\n"
    "        self.user_accounts: Dict[str, UserAccount] = {}\n",
    "        self.official_availability: Dict[str, OfficialAvailability] = {}\n"
    "        self.installation_state: Dict[str, InstallationState] = {}\n"
    "        self.user_accounts: Dict[str, UserAccount] = {}\n",
)
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "        self._counters: Dict[str, count] = {}\n",
    "        self._counters: Dict[str, count] = {}\n"
    "        self._lock = threading.RLock()\n",
)
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "    @contextmanager\n"
    "    def transaction(self):\n"
    "        \"\"\"No-op for the in-memory store (single-process, by reference).\"\"\"\n"
    "        yield\n",
    "    @contextmanager\n"
    "    def transaction(self):\n"
    "        \"\"\"Serialize atomic service operations in the threaded test/demo store.\"\"\"\n"
    "        with self._lock:\n"
    "            yield\n",
)
replace(
    "backend/hockey_scheduler/store/memory_store.py",
    "    # -- user accounts (#67) -------------------------------------------------\n",
    "    # -- installation claim state (#174) -----------------------------------\n"
    "    def add_installation_state(\n"
    "            self, state: InstallationState) -> InstallationState:\n"
    "        self.installation_state[state.id] = state\n"
    "        return state\n\n"
    "    def get_installation_state(\n"
    "            self, state_id: str) -> Optional[InstallationState]:\n"
    "        return self.installation_state.get(state_id)\n\n"
    "    # -- user accounts (#67) -------------------------------------------------\n",
)

# -- SQL store ------------------------------------------------------------
replace(
    "backend/hockey_scheduler/store/sql_store.py",
    "    GuardianLink,\n    RosterRole,\n",
    "    GuardianLink,\n    InstallationState,\n    RosterRole,\n",
)
replace(
    "backend/hockey_scheduler/store/sql_store.py",
    "    NotificationPreference: Spec(\n"
    "        NotificationPreference, \"notification_preferences\",\n"
    "        {\"channel\": _enum(NotificationChannel), \"enabled\": _bool()}),\n"
    "    UserAccount: Spec(UserAccount, \"user_accounts\",\n",
    "    NotificationPreference: Spec(\n"
    "        NotificationPreference, \"notification_preferences\",\n"
    "        {\"channel\": _enum(NotificationChannel), \"enabled\": _bool()}),\n"
    "    InstallationState: Spec(\n"
    "        InstallationState, \"installation_state\",\n"
    "        {\"claimed_at\": _dt()}),\n"
    "    UserAccount: Spec(UserAccount, \"user_accounts\",\n",
)
replace(
    "backend/hockey_scheduler/store/sql_store.py",
    "    # -- user accounts (#67) -------------------------------------------------\n",
    "    # -- installation claim state (#174) -----------------------------------\n"
    "    def add_installation_state(self, state): return self._insert(state)\n"
    "    def get_installation_state(self, state_id):\n"
    "        return self._get(InstallationState, state_id)\n\n"
    "    # -- user accounts (#67) -------------------------------------------------\n",
)

# -- web routes -----------------------------------------------------------
replace(
    "backend/hockey_scheduler/web/server.py",
    "from ..bootstrap import bootstrap_admin_from_env\n",
    "from ..bootstrap import (\n"
    "    bootstrap_admin_from_env,\n"
    "    claim_installation,\n"
    "    installation_claim_status,\n"
    ")\n",
)
replace(
    "backend/hockey_scheduler/web/server.py",
    "from ..domain import ROLE_LABELS, Permission, Role, can, permissions_for\n",
    "from ..domain import (\n"
    "    DomainError, ROLE_LABELS, Permission, Role, can, permissions_for,\n"
    ")\n",
)
replace(
    "backend/hockey_scheduler/web/server.py",
    "    \"game_cancelled\": 409,\n",
    "    \"game_cancelled\": 409,\n"
    "    \"already_claimed\": 409,\n"
    "    \"invalid_setup_code\": 401,\n"
    "    \"claim_unavailable\": 403,\n",
)
replace(
    "backend/hockey_scheduler/web/server.py",
    "        if path in (\"/\", \"\", \"/mobile\", \"/mobile/\"):\n"
    "            # index.html is the single responsive shell (#118) — it already\n"
    "            # covers phone-width viewports (login screen, full nav, no tab\n"
    "            # drift), so /mobile serves the same file rather than a second,\n"
    "            # divergent copy that goes stale and dead-ends when signed out.\n"
    "            rel = \"index.html\"\n"
    "        else:\n",
    "        if path in (\"/setup\", \"/setup/\"):\n"
    "            # The one-time production claim is deliberately isolated from\n"
    "            # the normal application/login shell (#174 PR B).\n"
    "            rel = \"setup.html\"\n"
    "        elif path in (\"/\", \"\", \"/mobile\", \"/mobile/\"):\n"
    "            # index.html is the single responsive shell (#118) — it already\n"
    "            # covers phone-width viewports (login screen, full nav, no tab\n"
    "            # drift), so /mobile serves the same file rather than a second,\n"
    "            # divergent copy that goes stale and dead-ends when signed out.\n"
    "            rel = \"index.html\"\n"
    "        else:\n",
)
replace(
    "backend/hockey_scheduler/web/server.py",
    "        if path == \"/api/status\":\n",
    "        if path == \"/api/bootstrap/status\":\n"
    "            # Public-safe one-time claim posture (#174). No account count,\n"
    "            # database details, usernames, or configuration values.\n"
    "            if self._rate_limited(\"bootstrap_status\", limit=60, window_seconds=60):\n"
    "                return\n"
    "            return self._send_json(installation_claim_status(\n"
    "                api, os.environ, _app_mode()))\n"
    "        if path == \"/api/status\":\n",
)
replace(
    "backend/hockey_scheduler/web/server.py",
    "        if path == \"/api/auth/login\":\n",
    "        if path == \"/api/bootstrap/claim\":\n"
    "            # The only anonymous setup mutation (#174): fresh production,\n"
    "            # durable store, configured one-time code, and zero prior claim.\n"
    "            # Rate-limit before code comparison; neither submitted secret is\n"
    "            # logged, echoed, persisted, or placed in the URL.\n"
    "            if self._rate_limited(\"bootstrap_claim\", limit=5, window_seconds=60):\n"
    "                return\n"
    "            try:\n"
    "                account = claim_installation(\n"
    "                    api, os.environ, _app_mode(),\n"
    "                    body.get(\"setup_code\", \"\"),\n"
    "                    body.get(\"username\", \"\"),\n"
    "                    body.get(\"password\", \"\"))\n"
    "            except DomainError as exc:\n"
    "                return self._send_api(exc.to_dict())\n"
    "            token = SESSIONS.login(\n"
    "                api.store, account.id,\n"
    "                user_agent=self.headers.get(\"User-Agent\"))\n"
    "            sess = SESSIONS.resolve(api.store, token)\n"
    "            cookie = self._session_cookie(token, SESSION_MAX_AGE)\n"
    "            return self._send_json(\n"
    "                {\"user\": user_view(sess, api.store)},\n"
    "                extra_headers=[(\"Set-Cookie\", cookie)])\n"
    "        if path == \"/api/auth/login\":\n",
)

# -- production documentation --------------------------------------------
append_once(
    "docs/architecture/production-runbook.md",
    "## One-time client-owned admin claim",
    """
## One-time client-owned admin claim

For the normal client-owned setup path, do **not** configure
`BOOTSTRAP_ADMIN_PASSWORD`. Generate a separate high-entropy one-time code and
place it in the deployment secret manager:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```text
APP_MODE=production
DATABASE_URL=<durable Postgres or real SQLite file>
INITIAL_SETUP_CODE=<generated one-time code>
```

Deliver the code to the client through a secure channel. The client opens
`/setup`, enters that code, and chooses their own League Admin username and
password. The server compares the code in constant time, atomically creates one
admin, writes only non-secret claim metadata/audit, and establishes a normal
secure session. The setup code and password are never returned or persisted.

Operational checks:

```text
GET /api/bootstrap/status
```

- `claim_available: true` means the fresh durable installation is ready.
- `reason: already_claimed` means use the normal sign-in page.
- Other unavailable reasons require deployment configuration/readiness review.

After a successful claim, remove `INITIAL_SETUP_CODE` from deployment
configuration. Reusing the code cannot create another admin because the durable
single-row claim marker and existing-account check fail closed with HTTP 409.
The environment/CLI bootstrap remains an emergency path and consumes the same
atomic marker, so browser and operations bootstrap cannot race.
""",
)
