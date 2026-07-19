"""Zero-dependency HTTP server for the iPhone-framed demo.

    python3 -m hockey_scheduler.web

Then open http://localhost:8000 in any browser (works on Windows — no Mac or
Xcode needed). The page renders an iPhone frame and drives the *real* roster /
substitute engine through the same :class:`ApiService` used by the tests.
"""

import json
import os
import re
import ssl
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from datetime import datetime, timedelta

from http.cookies import SimpleCookie

from ..api import ApiService
from ..bootstrap import (
    bootstrap_admin_from_env,
    claim_installation,
    installation_claim_status,
)
from ..domain import (
    AvailabilityStatus, DomainError, ROLE_LABELS, Permission, Role, can,
    permissions_for,
)
from ..full_demo import build_full_demo_store
from ..services import (
    delivery_loop_from_env,
    email_transport_from_env,
    push_transport_from_env,
)
from ..store import SqlStore, create_store
from .auth import (
    DEFAULT_TTL_SECONDS,
    DEMO_PASSWORD,
    DEMO_USERS,
    SESSION_COOKIE,
    SessionManager,
    user_view,
)
from .authz import authorize, required_permission
from ..api import v1_setup_adapter as _v1
from ..api import v2_setup_projection as _v2p
from .rate_limit import LoginThrottle, RateLimiter
from .scope import can_read_private_game_data, own_team_id, scope_violation
from .validation import BodyError, check_body, parse_json_object

# Acting role resolution (#50): a server-issued session cookie is authoritative.
# The X-Demo-Role header remains only as a dev fallback for scripts/curl; when
# neither is present we assume the full operator (League Admin) for convenience.
# APP_MODE=production (#68) turns both of those off: a valid session is
# required for anything gated, and the demo account seed is skipped.
ROLE_HEADER = "X-Demo-Role"


def _app_mode() -> str:
    """Read APP_MODE fresh on every call (not cached) so tests and ops can
    flip it without a process restart. Anything other than the literal
    "production" is treated as the permissive demo posture — the default
    must stay backward-compatible with the existing demo behavior.
    """
    return (os.environ.get("APP_MODE") or "demo").strip().lower()


def _trust_proxy_headers() -> bool:
    """Whether to trust ``X-Forwarded-For`` for the caller's real IP (#131,
    #132 review).

    Defaults to False: unlike ``X-Forwarded-Proto`` (used only for a cookie
    security nicety), ``X-Forwarded-For`` feeds directly into rate-limiting —
    an anonymous caller with no proxy in front of them can set this header to
    whatever they like on every request, so trusting it unconditionally would
    let them defeat rate limiting entirely by rotating a fake value each
    time. Only set ``TRUST_PROXY_HEADERS=1`` when this process is actually
    deployed behind a reverse proxy configured to strip/overwrite any
    client-supplied ``X-Forwarded-For`` before appending its own — i.e. the
    header is verified to represent the proxy's own view of the connection,
    not client-controlled input.
    """
    return (os.environ.get("TRUST_PROXY_HEADERS") or "").strip().lower() in ("1", "true", "yes")


def _allow_production_factory_reset() -> bool:
    """Whether the guarded production factory-reset workflow is reachable at
    all (#256). Read fresh on every call, same idiom as ``_app_mode()``/
    ``_trust_proxy_headers()``, so ops can flip it without a restart.
    Defaults to False: this is a whole-installation data-destroying action
    and must be an explicit, deliberate deployment opt-in, never an
    accidental default.
    """
    return (os.environ.get("ALLOW_PRODUCTION_FACTORY_RESET") or "").strip().lower() in (
        "1", "true", "yes")

# Sessions live outside DemoState so signing in survives a demo-data reset.
SESSIONS = SessionManager()

# Rate limiting for anonymous routes (#131) — outside DemoState for the same
# reason SESSIONS is: a demo-data reset should not hand every caller a fresh
# limit. DemoState.reset() clears it explicitly instead, so tests (which
# reset demo state between classes) start each suite with a clean slate.
RATE_LIMITER = RateLimiter()

# Login throttle (#267): failed-attempt lockout by source IP AND normalized
# username, separate from the anonymous-route RateLimiter. Same lifetime as
# RATE_LIMITER; DemoState.reset() clears it too so each test class starts clean.
LOGIN_THROTTLE = LoginThrottle()

# Cookie lifetime mirrors the session TTL so the browser drops the cookie when
# the server-side session would already be expired (#76).
SESSION_MAX_AGE = DEFAULT_TTL_SECONDS

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# Maps structured domain-error codes to HTTP status codes (per api-contract.md).
ERROR_HTTP_STATUS = {
    "not_found": 404,
    "validation_error": 400,
    "roster_locked": 409,
    "already_selected": 409,
    "not_enrolled": 409,
    "invalid_transition": 409,
    "slot_already_filled": 409,
    "not_eligible": 403,
    "forbidden": 403,
    "unauthorized": 401,
    "game_cancelled": 409,
    "has_dependencies": 409,  # delete refused: dependent records/history exist (#215)
    "already_claimed": 409,
    "invalid_setup_code": 401,
    "claim_unavailable": 403,
    "conflict": 409,               # DB integrity conflict, translated (#201)
    "concurrency_conflict": 409,   # serialization/deadlock/lock, retryable (#201)
    "malformed_json": 400,         # request body wasn't a JSON object (#271)
    "method_not_allowed": 405,     # known path, unsupported HTTP method (#271)
    "moved_to_v2": 409,            # v1 Player/Official CRUD moved to v2 (#271)
}

# Valid availability values, reused from the domain enum so the HTTP-layer
# schema check (#271) can't drift from what the service accepts.
_AVAILABILITY_VALUES = frozenset(s.value for s in AvailabilityStatus)


# Method-405 routing table (#271). Two ordered lists of compiled path patterns
# transcribed from the ``do_GET``/``do_POST`` dispatch below (an explicitly
# scoped near-term list, a bounded child of #202). ``_method_fallback`` uses
# them to answer an unsupported method with a JSON 405 + a correct ``Allow``
# header instead of the stdlib's HTML 501. Every method/path claimed here is
# verified against the real dispatch (see tests/test_write_schemas.py). The
# game POST pattern enumerates only the ACTUAL write actions, so a read-only
# subpath (``.../board``) is NOT claimed as POST.
_GET_ROUTES = [re.compile(p) for p in (
    r"^/api/demo/overview$",
    r"^/api/setup/hierarchy$",
    r"^/api/import/hierarchy-codes$",
    r"^/api/setup/leagues/[^/]+/teams$",
    r"^/api/setup/seasons/[^/]+/team-registrations$",
    r"^/api/onboarding/status$",
    r"^/api/v2/setup/overview$",
    r"^/api/v2/setup/hierarchy$",
    r"^/api/v2/setup/programs/[^/]+/teams$",
    r"^/api/v2/setup/seasons/[^/]+/team-registrations$",
    r"^/api/v2/setup/seasons/[^/]+/venue-access$",
    r"^/api/v2/onboarding/status$",
    r"^/api/bootstrap/status$",
    r"^/api/status$",
    r"^/api/health$",
    r"^/api/readiness$",
    r"^/api/auth/roles$",
    r"^/api/auth/accounts$",
    r"^/api/accounts$",
    r"^/api/accounts/[^/]+/sessions$",
    r"^/api/guardians/links$",
    r"^/api/officials$",
    r"^/api/players$",
    r"^/api/reschedule/pending$",
    r"^/api/officials/[^/]+/availability$",
    r"^/api/notifications$",
    r"^/api/notifications/deliveries$",
    r"^/api/notifications/contacts$",
    r"^/api/notifications/preferences$",
    r"^/api/calendar-feeds$",
    r"^/api/notifications/device-tokens$",
    r"^/api/me/assignments$",
    r"^/api/me/player-home$",
    r"^/api/me/substitute-opportunities/[^/]+$",
    r"^/api/me/guardian/home$",
    r"^/api/me/guardian/[^/]+/substitute-opportunities/[^/]+$",
    r"^/api/standings/[^/]+$",
    r"^/api/standings/league-season/[^/]+/[^/]+$",
    r"^/api/public/schedule$",
    r"^/api/public/standings/[^/]+$",
    r"^/api/public/standings/league-season/[^/]+/[^/]+$",
    r"^/api/public/games/[^/]+$",
    r"^/api/scheduler/drafts$",
    r"^/api/auth/me$",
    r"^/api/games/[^/]+(?:/(?:board|lineups|roster-status|roster|substitutes"
    r"|substitute-candidates|substitute-addable|reschedule|officials"
    r"|availability-summary))?$",
)]
_POST_ROUTES = [re.compile(p) for p in (
    r"^/api/bootstrap/claim$",
    r"^/api/auth/login$",
    r"^/api/auth/logout$",
    r"^/api/public/calendar-feeds$",
    r"^/api/notifications/preferences$",
    r"^/api/officials/[^/]+/availability$",
    r"^/api/officials/availability/[^/]+/delete$",
    r"^/api/games/[^/]+/reschedule/[^/]+/respond$",
    r"^/api/calendar-feeds$",
    r"^/api/calendar-feeds/[^/]+/revoke$",
    r"^/api/me/substitute-opportunities/[^/]+/"
    r"(?:enroll|withdraw|accept-offer|decline-offer)$",
    r"^/api/me/guardian/[^/]+/games/[^/]+/availability$",
    r"^/api/me/guardian/[^/]+/substitute-opportunities/[^/]+/"
    r"(?:accept-offer|decline-offer)$",
    r"^/api/reset$",
    r"^/api/demo/(?:reset|load|clear)$",
    r"^/api/admin/factory-reset/(?:preview|execute)$",
    r"^/api/demo/add-ice-slot$",
    # v1 setup writes (#271): EXACT routes transcribed from _handle_setup /
    # _handle_reassign — a broad `.+` over-claims POST on nonexistent paths
    # whose real POST is a 404, so the fallback would wrongly answer 405.
    r"^/api/setup/(?:league|season|level|division|club|team|organization"
    r"|venue|rink|ice-slot|game|official|player)$",  # entity creates
    r"^/api/setup/(?:organization|league|season|level|division|club|team"
    r"|venue|rink|ice-slot|game|player|official)/[^/]+/delete$",  # deletes
    r"^/api/setup/league/[^/]+/assign-organization$",
    r"^/api/setup/venue/[^/]+/assign-organization$",
    r"^/api/setup/rink/[^/]+/assign-venue$",
    r"^/api/setup/division/[^/]+/assign-level$",
    r"^/api/setup/team/[^/]+/assign-club$",
    r"^/api/setup/player/[^/]+/assign-team$",
    r"^/api/setup/seasons/[^/]+/team-registrations$",
    r"^/api/setup/seasons/[^/]+/roll-forward$",
    r"^/api/setup/season-team-registration/[^/]+/assign-division$",
    r"^/api/setup/season-team-registration/[^/]+/remove$",
    # v2 setup writes (#271): EXACT routes from _handle_setup_v2 /
    # _handle_reassign_v2 (the v2 entity/target sets differ from v1).
    r"^/api/v2/setup/(?:program|season|league|division|club|team|organization"
    r"|venue|rink|ice-slot|game|official|player)$",  # entity creates
    r"^/api/v2/setup/(?:organization|program|season|league|division|club|team"
    r"|venue|rink|ice-slot|game|official|player)/[^/]+/delete$",  # deletes
    r"^/api/v2/setup/program/[^/]+/assign-organization$",
    r"^/api/v2/setup/division/[^/]+/assign-league$",
    r"^/api/v2/setup/team/[^/]+/assign-club$",
    r"^/api/v2/setup/team/[^/]+/assign-league$",
    r"^/api/v2/setup/player/[^/]+/assign-team$",
    r"^/api/v2/setup/rink/[^/]+/assign-venue$",
    r"^/api/v2/setup/venue/[^/]+/assign-organization$",
    r"^/api/v2/setup/seasons/[^/]+/team-registrations$",
    r"^/api/v2/setup/seasons/[^/]+/venue-access$",
    r"^/api/v2/setup/seasons/[^/]+/roll-forward$",
    r"^/api/v2/setup/season-team-registration/[^/]+/assign-league$",
    r"^/api/v2/setup/season-team-registration/[^/]+/assign-division$",
    r"^/api/v2/setup/season-team-registration/[^/]+/remove$",
    r"^/api/v2/setup/season-team-registration/[^/]+/delete$",
    r"^/api/v2/setup/season-venue-access/[^/]+/remove$",
    r"^/api/v2/setup/season-venue-access/[^/]+/delete$",
    r"^/api/import/dry-run$",
    r"^/api/import/commit/(?:teams-players|officials-availability"
    r"|rinks-ice-slots)$",
    r"^/api/scheduler/draft$",
    r"^/api/scheduler/commit$",
    r"^/api/scheduler/drafts/(?:publish|discard)$",
    r"^/api/notifications/deliveries/process$",
    r"^/api/notifications/deliveries/[^/]+/(?:retry|ignore)$",
    r"^/api/notifications/contacts$",
    r"^/api/notifications/contacts/[^/]+/active$",
    r"^/api/notifications/device-tokens$",
    r"^/api/notifications/device-tokens/[^/]+/active$",
    r"^/api/notifications/preferences/[^/]+/active$",
    r"^/api/accounts$",
    r"^/api/accounts/[^/]+/active$",
    r"^/api/accounts/[^/]+/scope$",
    r"^/api/accounts/[^/]+/sessions/[^/]+/revoke$",
    r"^/api/guardians/links$",
    r"^/api/guardians/links/[^/]+/verify$",
    r"^/api/notifications/read-all$",
    r"^/api/notifications/[^/]+/read$",
    r"^/api/officials/assignments/[^/]+/(?:accept|decline|unassign)$",
    # Game write actions only (NOT the read-only subpaths above): a POST to a
    # read subpath like ``.../board`` is a 404, never a real route.
    r"^/api/games/[^/]+/(?:availability|availability/remind|build-roster"
    r"|roster/select|roster/remove|roster/copy-previous|officials/assign"
    r"|result|result/approve|publish|move|reschedule/request"
    r"|reschedule/[^/]+/decide|substitutes/enroll|substitutes/withdraw"
    r"|substitutes/add-candidate|substitutes/[^/]+/(?:offer|accept|decline"
    r"|add-to-roster)|roster/lock|roster/unlock|cancel)$",
)]


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class DemoState:
    """Holds the demo facade; can (re)build or clear itself for the demo."""

    def __init__(self) -> None:
        # Demo mode boots to a CLEAN SLATE (#215): an empty setup the operator
        # can Load sample data into or build by hand. (Production ignores `seed`
        # and preserves its durable store.)
        self.reset(seed=False)

    def _make_api(self, store):
        # Email/push transports come from EMAIL_MODE / SMTP_* (#63) and
        # PUSH_MODE / PUSH_* (#64) env; both dry-run by default, so nothing
        # sends real mail or push unless explicitly wired.
        return ApiService(
            store, email_transport=email_transport_from_env(os.environ),
            push_transport=push_transport_from_env(os.environ))

    def reset(self, actor_id: Optional[str] = None, seed: bool = True,
              audit_action: str = "demo_reset") -> None:
        """Rebuild the demo dataset (#215).

        ``seed=True`` builds the full canonical Alpine scenario (Load / Reset);
        ``seed=False`` produces a CLEAN SLATE — an empty setup with only the
        League-Admin persona so the operator can auto-sign-in and build (or Load)
        from scratch (Clear, and the demo's own cold boot).

        Demo-mode assumption: this is a single-operator action, not serialized
        against concurrent in-flight mutations on the threaded demo server. What
        we DO guarantee is atomicity and clean resource handling: the new dataset
        is built fully before any live reference is swapped, non-database side
        effects (rate-limit slate) are applied only on success, the superseded
        store is closed after a successful swap, and a half-built store is closed
        on failure. So a mid-build failure leaves the previous dataset — and the
        live self.api — untouched, and repeats don't leak connections.
        """
        store = create_store()  # SqlStore.__init__ applies pending numbered migrations (#75)
        old = getattr(self, "api", None)
        old_store = old.store if old is not None else None

        # Production (#71): NEVER reset the schema or seed demo data — that
        # would wipe a persistent DATABASE_URL store on every boot. Preserve
        # whatever is there and only bootstrap the first admin from env if the
        # store has no accounts yet.
        if _app_mode() == "production":
            self.api = self._make_api(store)
            self.game_id = None
            self.ids = {}
            bootstrap_admin_from_env(self.api, os.environ)
            RATE_LIMITER.reset()  # (#131) fresh process → clean rate-limit slate
            LOGIN_THROTTLE.reset()  # (#267) clear login lockouts on rebuild too
            self._close_store(old_store, store)
            return

        # The whole (re)build is ATOMIC (#215): for a SQL store it runs inside a
        # single transaction (the reentrant transaction() makes reset_schema +
        # every @_transactional seed call commit or roll back as one unit), and
        # for the in-memory store we build into a fresh object and only swap the
        # live references on success.
        def _build():
            if isinstance(store, SqlStore):
                store.reset_schema()
            api = self._make_api(store)
            if seed:
                _, game_id, ids = build_full_demo_store(store)
                self._seed_demo_accounts(api, ids)
            else:
                game_id, ids = None, {}
                self._seed_admin_account(api)  # clean slate keeps a sign-in path
            if actor_id:
                # One audit row, written after the new baseline exists, naming
                # the authenticated actor and the action (#215).
                api.setup.record_demo_event(audit_action, actor_id)
            return api, game_id, ids

        try:
            if isinstance(store, SqlStore):
                with store.transaction():
                    api, game_id, ids = _build()
            else:
                api, game_id, ids = _build()
        except Exception:
            # Failed build — discard the half-built store's connection and
            # leave the live state completely untouched.
            self._close_store(store, None)
            raise
        # Success — swap the live references in one step, only now that the new
        # baseline is fully built. Then apply the non-DB side effect and release
        # the superseded store.
        self.api, self.game_id, self.ids = api, game_id, ids
        RATE_LIMITER.reset()  # (#131) only after the (re)build actually succeeded
        LOGIN_THROTTLE.reset()  # (#267) login lockouts reset with the rebuild
        self._close_store(old_store, store)

    def _seed_admin_account(self, api) -> None:
        """The clean-slate demo keeps just the League-Admin persona so the demo
        still auto-signs-in and the operator can Load sample data or build by
        hand. Deterministic id keeps an existing admin session valid across a
        Clear/Load cycle."""
        api.accounts.create_account(
            "admin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
            actor_id="demo_seed", account_id="user_admin")

    @staticmethod
    def _close_store(store, keep) -> None:
        """Close ``store`` unless it's the one we're keeping (or None)."""
        if store is not None and store is not keep:
            try:
                store.close()
            except Exception:
                pass

    def _seed_demo_accounts(self, api, ids: dict) -> None:
        """Create the six demo personas as real, operator-created accounts
        (#67) — deterministic ids so existing sessions survive a reset (the
        session cookie carries role/scope already, so this only matters for
        display and for `/api/accounts` listings). Seeds into the passed-in
        ``api`` (the freshly built one), not ``self.api``, so it runs before the
        live-reference swap and stays inside the atomic reseed (#215).
        """
        scopes = {
            "coach": {"team_id": ids.get("home_team_id")},
            "player": {"team_id": ids.get("home_team_id"),
                      "player_id": ids.get("selected_player_id")},
            "official": {"official_id": ids.get("referee_id")},
        }
        for username, role in DEMO_USERS.items():
            api.accounts.create_account(
                username, DEMO_PASSWORD, role, scope=scopes.get(username, {}),
                actor_id="demo_seed", account_id=f"user_{username}")

        # Link+verify the guardian persona to a junior (U16) player (#26) so
        # the guardian linked-junior workflow works out of the box. A guardian
        # holds no session scope; authority comes solely from this verified
        # link. Deterministic link id keeps demo audit ids stable across resets.
        junior_id = ids.get("selected_player_id")
        if junior_id:
            api.guardians.link_guardian(
                "user_guardian", junior_id, verified=True,
                actor_id="demo_seed", link_id="glink_demo")


STATE = DemoState()


class Handler(BaseHTTPRequestHandler):
    # Quieter logging.
    def log_message(self, *args):  # noqa: D401
        pass

    # -- helpers -----------------------------------------------------------
    def _security_headers(self) -> None:
        """Baseline hardening headers on every response (API and static)."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def _send_json(self, payload, code: int = 200, extra_headers=None) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # API payloads can be per-session (feed, scope) — never cache them.
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_ics(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", "inline; filename=calendar.ics")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _own_feed_actor(self, role, scope, actor_type, actor_ref) -> bool:
        """Whether the signed-in user owns (team/official/player, ref) — used to
        let a non-operator manage only their own calendar feeds (#82).

        Role-specific, not just scope-based: a player carries ``team_id`` in
        scope but does NOT own the shared team feed (that is the coach's), so
        each actor_type is gated on the matching role. Otherwise a player could
        create/revoke a coach's ``team:<id>`` feed via the API — the same
        shared-resource bug fixed for preferences in #81."""
        if actor_type == "official":
            return role == Role.OFFICIAL and scope.get("official_id") == actor_ref
        if actor_type == "team":
            return role == Role.COACH and scope.get("team_id") == actor_ref
        if actor_type == "player":
            return role == Role.PLAYER and scope.get("player_id") == actor_ref
        return False

    def _feed_guard(self, actor_type, actor_ref) -> bool:
        """Send 401/403 and return True if the caller may not manage feed tokens
        for (actor_type, actor_ref): operator manages any, others only own (#82)."""
        role, scope, _uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if can(role, Permission.MANAGE_SCHEDULE):
            return False
        if actor_ref and self._own_feed_actor(role, scope, actor_type, actor_ref):
            return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "You can only manage your own calendar feeds."}}, 403)
        return True

    def _reschedule_opponent_guard(self, request_id) -> bool:
        """Send 401/403 and return True if the caller may not respond to
        this reschedule request (#29): an operator (MANAGE_SCHEDULE) may
        respond to any; a coach only if their team is the OPPONENT of the
        team that requested it — not the requester itself, and not an
        unrelated team's coach. scope.py's generic team_id body-key check
        doesn't cover this (the respond action carries no team_id), so it's
        enforced here instead."""
        role, scope, _uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if can(role, Permission.MANAGE_SCHEDULE):
            return False
        req = STATE.api.store.get_reschedule_request(request_id)
        game = STATE.api.store.get_game(req.game_id) if req else None
        if req is not None and game is not None:
            opponent_id = (game.away_team_id
                          if req.requested_by_team_id == game.home_team_id
                          else game.home_team_id)
            if role == Role.COACH and scope.get("team_id") == opponent_id:
                return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "Only the opponent team's coach (or an operator) can "
                       "respond to this reschedule request."}}, 403)
        return True

    def _official_guard(self, official_id) -> bool:
        """Send 401/403 and return True if the caller may not manage
        ``official_id``'s availability: an operator (MANAGE_SCHEDULE) may manage
        any, an official only their own (#88)."""
        role, scope, _uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if can(role, Permission.MANAGE_SCHEDULE):
            return False
        if official_id and scope.get("official_id") == official_id:
            return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "You can only manage your own availability."}}, 403)
        return True

    def _cookie(self, name: str):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _cookie_is_secure(self) -> bool:
        """Whether the session cookie should carry the ``Secure`` flag (#76).

        Secure in production (deployments are always HTTPS) and whenever the
        request itself arrived over TLS — directly, or via a reverse proxy that
        terminates TLS and forwards the original scheme in
        ``X-Forwarded-Proto``. In the local/demo HTTP posture it is omitted so
        the cookie is still accepted over plain http://localhost.
        """
        if _app_mode() == "production":
            return True
        # Proxy-forwarded scheme: X-Forwarded-Proto (de-facto) or RFC 7239
        # Forwarded: proto=https. Take the first hop only.
        xfp = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0]
        if xfp.strip().lower() == "https":
            return True
        fwd = (self.headers.get("Forwarded") or "").split(",")[0].lower()
        if "proto=https" in fwd:
            return True
        # Direct TLS: the request arrived on an ssl-wrapped socket.
        return isinstance(getattr(self, "connection", None), ssl.SSLSocket)

    def _session_cookie(self, token: str, max_age: int) -> str:
        """Build a session Set-Cookie value with consistent security attributes.

        HttpOnly + SameSite=Lax always; Secure conditionally (see
        ``_cookie_is_secure``). Used for both issuing and clearing the cookie so
        the two always share the same attributes (a browser only replaces a
        cookie when Path/Secure match).
        """
        parts = [f"{SESSION_COOKIE}={token}", "HttpOnly", "Path=/",
                 "SameSite=Lax", f"Max-Age={max_age}"]
        if self._cookie_is_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _resolve_role(self):
        """Resolve the acting identity. Returns (role, scope, user_id, err).

        Demo mode (default): a valid session cookie is authoritative, else the
        X-Demo-Role dev fallback (invalid → 403, no scope), else signed out
        (401). The old headerless League Admin convenience is now opt-in via
        DEMO_HEADERLESS_ADMIN=1 so that logout is meaningful and cookieless
        callers are not silently admin. A *present* session cookie that is
        invalid/expired is rejected (401) rather than silently downgraded.

        Production mode (APP_MODE=production, #68): the session cookie is
        still authoritative, but there is no fallback at all — no
        X-Demo-Role (it is never even read), no headerless League Admin.
        Every request without a valid session is rejected (401).

        ``user_id`` is the signed-in account's id when a real session backs
        the request, else None (the X-Demo-Role/headerless demo paths have no
        backing account). It drives per-user notification read state (#69):
        a real session always gets its own read receipts; the identity-less
        demo fallbacks share a coarser role/scope-derived bucket.
        """
        sid = self._cookie(SESSION_COOKIE)
        if sid is not None:
            sess = SESSIONS.resolve(STATE.api.store, sid)
            if sess is None:
                return None, None, None, (401, {"error": {
                    "code": "unauthorized",
                    "message": "Session expired — please sign in again."}})
            return sess["role"], sess.get("scope", {}), sess.get("user_id"), None
        if _app_mode() == "production":
            return None, None, None, (401, {"error": {
                "code": "unauthorized",
                "message": "Sign in required."}})
        raw_role = self.headers.get(ROLE_HEADER)
        if raw_role is None or raw_role == "":
            # No session and no explicit X-Demo-Role. The old headerless League
            # Admin convenience is a footgun: it makes logout meaningless (a
            # cookieless request silently becomes admin) and hides missing auth
            # in tests. It is now opt-in via DEMO_HEADERLESS_ADMIN=1; otherwise
            # a cookieless request is simply signed out (401), the same posture
            # as production minus the explicit dev-role escape hatch below.
            if os.environ.get("DEMO_HEADERLESS_ADMIN") == "1":
                return Role.LEAGUE_ADMIN, {}, None, None
            return None, None, None, (401, {"error": {
                "code": "unauthorized",
                "message": "Sign in required."}})
        try:
            return Role(raw_role), {}, None, None
        except ValueError:
            return None, None, None, (403, {"error": {
                "code": "forbidden",
                "message": f"Unknown role '{raw_role}'.",
                "details": {"role": raw_role}}})

    def _send_api(self, payload) -> None:
        """Send an API payload, mapping structured domain errors to HTTP codes."""
        if isinstance(payload, dict) and "error" in payload:
            code = payload["error"].get("code", "domain_error")
            return self._send_json(payload, ERROR_HTTP_STATUS.get(code, 400))
        return self._send_json(payload)

    def _client_ip(self) -> str:
        """The caller's IP for rate-limiting purposes (#131, #132 review).

        Only honors ``X-Forwarded-For`` when ``TRUST_PROXY_HEADERS=1`` is
        explicitly set (``_trust_proxy_headers``) — this header feeds
        directly into rate-limiting, so trusting it unconditionally would
        let ANY anonymous caller (proxy or not) defeat the limiter entirely
        by sending a different value on every request. Without the deployer
        explicitly confirming a real proxy strips/overwrites client-supplied
        values first, the only trustworthy signal is the raw connecting
        socket address.
        """
        if _trust_proxy_headers():
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if xff:
                return xff
        return self.client_address[0] if self.client_address else "unknown"

    def _rate_limited(self, bucket: str, limit: int, window_seconds: float) -> bool:
        """Send 429 and return True if this caller has exceeded ``limit``
        requests to ``bucket`` in the last ``window_seconds`` (#131)."""
        key = self._client_ip()
        if RATE_LIMITER.allow(bucket, key, limit, window_seconds):
            return False
        self._send_json({"error": {
            "code": "rate_limited",
            "message": "Too many requests. Please slow down and try again "
                       "shortly."}}, 429)
        return True

    def _operator_only(self, guard: str) -> bool:
        """For read-only operator routes: send 401/403 and return True if the
        caller may not operate, else False. Same resolution as the feed —
        invalid cookie → 401, and the ``guard`` path's permission → 403 for
        non-operators.
        """
        role, scope, _uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if not authorize(role, guard):
            perm = required_permission(guard)
            self._send_json({"error": {
                "code": "forbidden",
                "message": (f"Your role ({ROLE_LABELS[role]}) can't do this "
                            f"(requires {perm.value})."),
                "details": {"role": role.value,
                            "required": perm.value if perm else None},
            }}, 403)
            return True
        return False

    def _require_player_scope(self):
        """Resolve the signed-in player's id + user id from the session for a
        player-scoped route (#110), so the browser never passes a player_id.
        Returns ``(player_id, user_id, None)`` when a bound player session is
        present, else ``(None, None, (status, payload))`` for the caller to
        send: no/expired cookie → 401, a valid session without a player
        binding → 403. Shared by the GET opportunity-detail and POST
        accept/decline routes."""
        sid = self._cookie(SESSION_COOKIE)
        if sid is None:
            return None, None, (401, {"error": {
                "code": "unauthorized",
                "message": "Sign in as a player to do this."}})
        sess = SESSIONS.resolve(STATE.api.store, sid)
        if sess is None:
            return None, None, (401, {"error": {
                "code": "unauthorized",
                "message": "Session expired — please sign in again."}})
        pid = sess.get("scope", {}).get("player_id")
        if not pid:
            return None, None, (403, {"error": {
                "code": "forbidden",
                "message": "This is only available to signed-in players."}})
        return pid, sess.get("user_id"), None

    def _require_guardian_scope(self):
        """Resolve the signed-in guardian's user id from the session for a
        guardian-scoped route (#26). Returns ``(guardian_user_id, None)`` for a
        guardian session, else ``(None, (status, payload))``: no/expired cookie
        → 401, a session whose role is not guardian → 403. The *link* between
        this guardian and a specific junior is checked separately, per action,
        so this only establishes "you are signed in as a guardian"."""
        sid = self._cookie(SESSION_COOKIE)
        if sid is None:
            return None, (401, {"error": {
                "code": "unauthorized",
                "message": "Sign in as a guardian to do this."}})
        sess = SESSIONS.resolve(STATE.api.store, sid)
        if sess is None:
            return None, (401, {"error": {
                "code": "unauthorized",
                "message": "Session expired — please sign in again."}})
        if sess.get("role") != Role.GUARDIAN.value:
            return None, (403, {"error": {
                "code": "forbidden",
                "message": "This is only available to signed-in guardians."}})
        uid = sess.get("user_id")
        if not uid:
            return None, (403, {"error": {
                "code": "forbidden",
                "message": "This is only available to signed-in guardians."}})
        return uid, None

    def _guardian_link_or_403(self, guardian_user_id, player_id):
        """Send 403 and return True when this guardian is not *verified* to act
        for ``player_id`` (#26) — the authority gate every guardian action
        passes before touching the junior's data. A non-link is reported as a
        plain forbidden without confirming the junior exists."""
        if STATE.api.guardians.is_verified_guardian(guardian_user_id, player_id):
            return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "You are not a verified guardian for this player."}}, 403)
        return True

    @staticmethod
    def _own_recipient_ref(role, scope, user_id=None):
        """The delivery recipient_ref the signed-in user speaks for (#81), used
        to let a non-operator manage only their own preferences.

        ``team:<id>`` is a single shared delivery target (the COACH audience) —
        one preference row governs every notification sent to that team. Only a
        coach speaks for it. A PLAYER must NOT map here: notification fan-out in
        this slice never targets an individual player, so a player has no own
        delivery channel to configure, and letting them edit ``team:<id>`` would
        let one player mute the whole team's notifications.

        A GUARDIAN speaks for ``guardian:<user_id>`` (#32) — the delivery
        recipient a linked junior's PLAYER-audience notifications now also fan
        out to. Unlike official/coach, a guardian's session carries no
        ``scope`` at all (#26 — "authority comes solely from the verified
        link"), so this is keyed off their own account id instead."""
        if role == Role.OFFICIAL and scope.get("official_id"):
            return "official:" + scope["official_id"]
        if role == Role.COACH and scope.get("team_id"):
            return "team:" + scope["team_id"]
        if role == Role.GUARDIAN and user_id:
            return "guardian:" + user_id
        return None

    def _prefs_guard(self, recipient_ref) -> bool:
        """Send 401/403 and return True if the caller may not manage
        ``recipient_ref``'s notification preferences (#81). An operator
        (MANAGE_SCHEDULE) may manage anyone; anyone else only their own."""
        role, scope, uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if can(role, Permission.MANAGE_SCHEDULE):
            return False
        if recipient_ref and recipient_ref == self._own_recipient_ref(role, scope, uid):
            return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "You can only manage your own notification preferences."}},
            403)
        return True

    def _read_json_object(self) -> dict:
        """Read the request body as a JSON object (#271).

        Replaces the old silent ``{}`` coercion of malformed JSON: an invalid or
        non-object body raises ``BodyError`` (surfaced by ``do_POST`` as a stable
        400) instead of quietly reaching business logic as an empty dict. An
        empty body (no Content-Length) stays ``{}`` — many routes take no body.
        """
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return parse_json_object(raw)

    def _serve_static(self, path: str) -> None:
        if path in ("/setup", "/setup/"):
            # The one-time production claim is deliberately isolated from
            # the normal application/login shell (#174 PR B).
            rel = "setup.html"
        elif path in ("/", "", "/mobile", "/mobile/"):
            # index.html is the single responsive shell (#118) — it already
            # covers phone-width viewports (login screen, full nav, no tab
            # drift), so /mobile serves the same file rather than a second,
            # divergent copy that goes stale and dead-ends when signed out.
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self.send_error(404, "Not found")
            return
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        if target.suffix == ".html":
            # The SPA loads one local script and stylesheet; inline styles are
            # used as style="" attributes, so style-src keeps 'unsafe-inline'.
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'self'; "
                             "form-action 'self'")
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        api = STATE.api
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        # Public iCal feed (#82/#33): bearer token in the URL scopes access; no
        # session. Unknown/revoked/mismatched token → 404 (don't confirm which).
        cal = re.match(r"^/calendar/(team|division|official|player)/([^/]+)\.ics$", path)
        if cal:
            # (#131) — a real calendar app polls this every 15-60 min per
            # subscription; a generous per-IP ceiling only catches abuse, not
            # normal use, even for someone subscribed to several feeds.
            if self._rate_limited("calendar_ics", limit=30, window_seconds=60):
                return
            ics = api.calendar_feed_ics(cal.group(1), cal.group(2))
            if ics is None:
                return self._send_json({"error": {
                    "code": "not_found", "message": "Calendar not found."}}, 404)
            return self._send_ics(ics)
        if path == "/api/demo/overview":
            return self._send_api(api.get_demo_overview())
        if path == "/api/setup/hierarchy":
            # Nested setup tree for the operator's mental model (#166 PR C).
            # It carries player_count leaves, so gate it like the player list
            # (MANAGE_SETUP) rather than leaving it on the unauthenticated
            # overview — even a count is roster-adjacent.
            if self._operator_only("/api/setup/player"):
                return
            return self._send_api(api.get_setup_hierarchy())
        if path == "/api/import/hierarchy-codes":
            # Existing Program/League/Venue codes for the hierarchy import
            # wizard's pickers (#260 review Q2/Q3/Q6) — same gate as the rest
            # of the hierarchy import surface.
            if self._operator_only("/api/import/hierarchy-codes"):
                return
            return self._send_api(api.get_hierarchy_import_codes())
        mlt = re.match(r"^/api/setup/leagues/([^/]+)/teams$", path)
        if mlt:
            # A league's permanent teams (#180). Operator-gated (MANAGE_SETUP)
            # like the rest of the setup surface — team names aren't PII but the
            # roster-adjacent read stays behind the same gate as the hierarchy.
            if self._operator_only("/api/setup/player"):
                return
            return self._send_api(api.list_program_teams(mlt.group(1)))
        mtr = re.match(r"^/api/setup/seasons/([^/]+)/team-registrations$", path)
        if mtr:
            # A season's team registrations (#180) — which permanent teams play
            # this season and in which division.
            if self._operator_only("/api/setup/player"):
                return
            # v1 boundary (#233 C1b): each registration carries the canonical
            # competition league_id — drop it from every row so this read route's
            # v1 shape is unchanged.
            listed = api.list_season_team_registrations(mtr.group(1))
            if isinstance(listed, dict) and "registrations" in listed:
                listed = {**listed, "registrations": [
                    _v1.registration_to_v1(r) for r in listed["registrations"]]}
            return self._send_api(listed)
        if path == "/api/onboarding/status":
            # Guided onboarding progress for the Setup wizard (#174 PR C). It
            # aggregates the full setup hierarchy plus account/player onboarding
            # state, so it is League-Admin-only (MANAGE_SETUP, which no other
            # role holds) — non-admins get 403. The payload is counts +
            # structural names only: no player names, usernames, or secrets.
            if self._operator_only("/api/onboarding/status"):
                return
            return self._send_api(api.get_onboarding_status(_app_mode()))
        # Canonical v2 setup reads (#233 Slice C2). Same MANAGE_SETUP operator
        # gate as their v1 siblings, but canonical keys and shapes — no v1
        # adapter mapping.
        if path == "/api/v2/setup/overview":
            # Canonical flat setup-entity lists for the Setup/Records UI (#233
            # Slice B2a). Gated MANAGE_ARENA (not MANAGE_SETUP like hierarchy)
            # — both League Admin and Arena Manager hold it, and Arena
            # Managers need the facility portion (organizations/venues/rinks)
            # for their own Setup cards; the payload has no PII/roster counts.
            if self._operator_only("/api/v2/setup/overview"):
                return
            return self._send_api(api.get_setup_overview_v2())
        if path == "/api/v2/setup/hierarchy":
            if self._operator_only("/api/v2/setup/player"):
                return
            return self._send_api(api.get_setup_hierarchy_v2())
        mpt = re.match(r"^/api/v2/setup/programs/([^/]+)/teams$", path)
        if mpt:
            if self._operator_only("/api/v2/setup/player"):
                return
            return self._send_api(api.list_program_teams_v2(mpt.group(1)))
        mv2tr = re.match(r"^/api/v2/setup/seasons/([^/]+)/team-registrations$", path)
        if mv2tr:
            if self._operator_only("/api/v2/setup/player"):
                return
            # Canonical: each registration row keeps its competition league_id.
            return self._send_api(
                api.list_season_team_registrations(mv2tr.group(1)))
        mv2va = re.match(r"^/api/v2/setup/seasons/([^/]+)/venue-access$", path)
        if mv2va:
            if self._operator_only("/api/v2/setup/player"):
                return
            return self._send_api(api.list_season_venue_access(mv2va.group(1)))
        if path == "/api/v2/onboarding/status":
            if self._operator_only("/api/v2/onboarding/status"):
                return
            return self._send_api(api.get_onboarding_status_v2(_app_mode()))
        if path == "/api/bootstrap/status":
            # Public-safe one-time claim posture (#174). No account count,
            # database details, usernames, or configuration values.
            if self._rate_limited("bootstrap_status", limit=60, window_seconds=60):
                return
            return self._send_json(installation_claim_status(
                api, os.environ, _app_mode()))
        if path == "/api/status":
            # Deployment posture for the UI status chips (#72): app mode +
            # store backend + email/push delivery modes. Non-sensitive, no
            # auth — like a health endpoint.
            status = api.runtime_status()
            status["app_mode"] = _app_mode()
            # Whether the demo setup is a clean slate (#215): drives the header's
            # Load-vs-Reset control and the "Start your league" empty state. Demo
            # mode only; never relevant in production.
            status["demo_empty"] = (_app_mode() != "production"
                                    and not api.store.all_programs()
                                    and not api.store.all_teams())
            # Whether the guarded production factory-reset workflow is actually
            # reachable (#256): the UI only surfaces the Administration → Danger
            # zone when BOTH conditions hold — the same gate the /execute route
            # enforces — so the control is never shown where it couldn't run.
            # Non-sensitive: it exposes only a deployment posture bit, never any
            # data or secret, exactly like app_mode above.
            status["factory_reset_enabled"] = (
                _app_mode() == "production" and _allow_production_factory_reset())
            return self._send_json(status)
        if path == "/api/health":
            # Liveness + dependency snapshot (#90). Public, non-sensitive.
            return self._send_json(api.get_health())
        if path == "/api/readiness":
            # Deployment readiness (#90). The cookie-hardening check must exercise
            # the *real* Secure-cookie decision (`_cookie_is_secure`, #76) rather
            # than re-deriving it from app_mode — deriving `mode == "production"`
            # here and again inside get_readiness only compares production-ness to
            # itself, so it can never fail even if cookie hardening regressed. In
            # production `_cookie_is_secure` is unconditionally True (deployments
            # are HTTPS); a regression that stopped hardening it would now fail
            # readiness instead of silently passing.
            return self._send_json(
                api.get_readiness(_app_mode(),
                                  cookie_hardened=self._cookie_is_secure()))
        if path == "/api/auth/roles":
            # Roles + their permissions so the UI can build the switcher and
            # gate actions from the same policy the server enforces (#24).
            return self._send_json({
                "default": Role.LEAGUE_ADMIN.value,
                "roles": [
                    {"id": r.value, "label": ROLE_LABELS[r],
                     "permissions": permissions_for(r)}
                    for r in Role
                ],
            })
        if path == "/api/auth/accounts":
            # The pickable, active accounts for the demo sign-in UI (#50/#67).
            # This is a demo login-picker convenience — in production it would
            # enumerate real usernames/roles without auth, so it returns an
            # empty list there (#68); the UI just shows a manual sign-in form.
            if _app_mode() == "production":
                return self._send_json({"accounts": []})
            rows = api.list_user_accounts().get("user_accounts", [])
            return self._send_json({"accounts": [
                {"username": a["username"], "role": a["role"],
                 "label": ROLE_LABELS[Role(a["role"])]}
                for a in rows if a["active"]]})
        if path == "/api/accounts":
            # Full account listing for operators (#67); same operator guard
            # as the delivery/contacts/device-token registries.
            if self._operator_only("/api/accounts"):
                return
            return self._send_api(api.list_user_accounts())
        ms = re.match(r"^/api/accounts/([^/]+)/sessions$", path)
        if ms:
            # An account's login sessions, League-Admin only (#78). No token
            # material is ever returned — only lifecycle metadata.
            if self._operator_only(path):
                return
            return self._send_api(api.list_account_sessions(ms.group(1)))
        if path == "/api/guardians/links":
            # Every guardian↔junior link, for the Users tab's admin surface
            # (#35). Same operator guard as account listing — this is
            # identity administration, not guardian-scoped self-service.
            if self._operator_only("/api/accounts"):
                return
            return self._send_api(api.list_guardian_links())
        if path == "/api/officials":
            return self._send_api({"officials": api.get_officials()})
        if path == "/api/players":
            # League-wide player list for the Setup "Players" card (#114) —
            # never bundled into /api/demo/overview: that endpoint is
            # unauthenticated and this app's own convention is that no player
            # name (a junior, in most divisions) is ever exposed without a
            # session. Gated the same as creating one (MANAGE_SETUP).
            if self._operator_only("/api/setup/player"):
                return
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            team_id = (qs.get("team_id") or [None])[0]
            return self._send_api(api.list_players(team_id))
        if path == "/api/reschedule/pending":
            # League-wide "awaiting my decision" queue (#29) — the opponent
            # has already accepted; a league admin/arena manager just needs
            # to see everything waiting on them across all games.
            if self._operator_only("/api/reschedule/pending"):
                return
            all_requests = api.list_reschedule_requests()  # a plain store read, never errors
            pending = [r for r in all_requests
                      if r["status"] == "pending_league_approval"]
            return self._send_api({"requests": pending})
        oav = re.match(r"^/api/officials/([^/]+)/availability$", path)
        if oav:
            # An official's declared availability windows (#88). Operator → any;
            # an official → only their own.
            if self._official_guard(oav.group(1)):
                return
            return self._send_api(api.list_official_availability(oav.group(1)))
        if path == "/api/notifications":
            # The signed-in user's feed (#32). Same resolution as POSTs: valid
            # session → its role/scope; invalid cookie → 401; else admin default.
            role, scope, user_id, err = self._resolve_role()
            if err is not None:
                code, payload = err
                return self._send_json(payload, code)
            return self._send_api(
                api.get_notifications(role.value, scope, user_id=user_id))
        if path == "/api/notifications/deliveries":
            # Delivery-queue overview for operators (#58). Exposes internal
            # queue state, so it is operator-only (invalid cookie → 401,
            # non-operator → 403 via the drain endpoint's permission).
            if self._operator_only("/api/notifications/deliveries/process"):
                return
            return self._send_api(api.get_delivery_overview())
        if path == "/api/notifications/contacts":
            # Contact registry listing for operators (#60); same guard.
            if self._operator_only("/api/notifications/contacts"):
                return
            return self._send_api(api.list_contact_destinations())
        if path == "/api/notifications/preferences":
            # A recipient's channel preferences (#81). Operator → any recipient;
            # a signed-in user → only their own. recipient_ref via query string.
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            recipient_ref = (qs.get("recipient_ref") or [""])[0]
            if self._prefs_guard(recipient_ref):
                return
            return self._send_api(api.get_notification_preferences(recipient_ref))
        if path == "/api/calendar-feeds":
            # List an actor's feed tokens (#82). Operator → any; user → own.
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            actor_type = (qs.get("actor_type") or [""])[0]
            actor_ref = (qs.get("actor_ref") or [""])[0]
            if self._feed_guard(actor_type, actor_ref):
                return
            return self._send_api(
                api.list_calendar_feed_tokens(actor_type, actor_ref))
        if path == "/api/notifications/device-tokens":
            # Device token registry listing for operators (#65); same guard.
            if self._operator_only("/api/notifications/device-tokens"):
                return
            return self._send_api(api.list_device_tokens())
        if path == "/api/me/assignments":
            # The signed-in official's own inbox (#55). Identity comes from the
            # session cookie, with the same rules as /api/auth/me: no cookie →
            # empty; a present-but-invalid/expired cookie → 401; a valid session
            # without an official binding → empty; a bound official → their inbox.
            sid = self._cookie(SESSION_COOKIE)
            if sid is None:
                return self._send_json({"official_id": None, "assignments": []})
            sess = SESSIONS.resolve(api.store, sid)
            if sess is None:
                return self._send_json({"error": {
                    "code": "unauthorized",
                    "message": "Session expired — please sign in again."}}, 401)
            oid = sess.get("scope", {}).get("official_id")
            if not oid:
                return self._send_json({"official_id": None, "assignments": []})
            return self._send_api(api.get_official_inbox(oid))
        if path == "/api/me/player-home":
            # The signed-in player's own home screen (#107). Same identity
            # rules as /api/me/assignments: no cookie -> empty; invalid/
            # expired cookie -> 401; a valid session without a player
            # binding -> empty; a bound player -> their home data.
            sid = self._cookie(SESSION_COOKIE)
            empty = {"player_id": None, "next_game": None, "today_count": 0,
                     "substitute_offers": [], "substitute_opportunities": [],
                     "unread_notifications": 0}
            if sid is None:
                return self._send_json(empty)
            sess = SESSIONS.resolve(api.store, sid)
            if sess is None:
                return self._send_json({"error": {
                    "code": "unauthorized",
                    "message": "Session expired — please sign in again."}}, 401)
            pid = sess.get("scope", {}).get("player_id")
            if not pid:
                return self._send_json(empty)
            return self._send_api(api.get_player_home(pid, user_id=sess.get("user_id")))
        mso = re.match(r"^/api/me/substitute-opportunities/([^/]+)$", path)
        if mso:
            # Detail for one substitute opportunity, scoped to the signed-in
            # player (#110). Identity comes from the session, never a query
            # param — same rules as /api/me/player-home, but a specific game
            # detail has no meaningful "empty" form, so an unauthenticated or
            # non-player caller gets a structured error rather than a stub.
            pid, _uid, err = self._require_player_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            return self._send_api(
                api.get_substitute_opportunity(pid, mso.group(1)))
        if path == "/api/me/guardian/home":
            # The signed-in guardian's linked-junior surface (#26): every
            # verified junior's Player Home payload. Identity is the guardian's
            # session; no junior id is passed by the browser. A non-guardian or
            # unauthenticated caller gets a structured error, not a stub.
            guid, err = self._require_guardian_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            return self._send_api(api.get_guardian_home(guid))
        mgo = re.match(
            r"^/api/me/guardian/([^/]+)/substitute-opportunities/([^/]+)$", path)
        if mgo:
            # Detail for one substitute opportunity, viewed by a guardian on
            # behalf of a linked junior (#26). The guardian must hold a verified
            # link to that junior; otherwise 403 without confirming the game.
            guid, err = self._require_guardian_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            jid = mgo.group(1)
            if self._guardian_link_or_403(guid, jid):
                return
            return self._send_api(
                api.get_substitute_opportunity(jid, mgo.group(2)))
        sd = re.match(r"^/api/standings/([^/]+)$", path)
        if sd:
            return self._send_api(api.get_standings(sd.group(1)))
        # #283 Slice D: LeagueSeason-wide standings (across all its Divisions),
        # keyed by (league_id, season_id). Exhibition games are excluded.
        lss = re.match(
            r"^/api/standings/league-season/([^/]+)/([^/]+)$", path)
        if lss:
            return self._send_api(api.get_league_season_standings(
                lss.group(1), lss.group(2)))
        # Public, no-auth surface (#83): schedule / standings / game detail,
        # public-safe fields only. Reachable unauthenticated in production.
        # Rate-limited (#131) — generous, since the public portal itself
        # fires several of these per page load/tab switch; the ceiling is
        # there to catch scraping/abuse, not normal browsing.
        if path == "/api/public/schedule":
            if self._rate_limited("public_read", limit=120, window_seconds=60):
                return
            return self._send_api(api.get_public_schedule())
        ps = re.match(r"^/api/public/standings/([^/]+)$", path)
        if ps:
            if self._rate_limited("public_read", limit=120, window_seconds=60):
                return
            return self._send_api(api.get_public_standings(ps.group(1)))
        pls = re.match(
            r"^/api/public/standings/league-season/([^/]+)/([^/]+)$", path)
        if pls:
            if self._rate_limited("public_read", limit=120, window_seconds=60):
                return
            return self._send_api(api.get_public_league_season_standings(
                pls.group(1), pls.group(2)))
        pg = re.match(r"^/api/public/games/([^/]+)$", path)
        if pg:
            if self._rate_limited("public_read", limit=120, window_seconds=60):
                return
            return self._send_api(api.get_public_game(pg.group(1)))
        if path == "/api/scheduler/drafts":
            # Current draft games for the review screen (#86), operator-only.
            if self._operator_only("/api/scheduler/commit"):
                return
            return self._send_api(api.list_draft_games())
        if path == "/api/auth/me":
            # Consistent with POST role resolution (#50): no cookie → signed out,
            # a valid cookie → the user, a present-but-invalid/expired cookie →
            # 401 (must re-auth) rather than silently reading as signed out.
            sid = self._cookie(SESSION_COOKIE)
            if sid is None:
                return self._send_json({"user": None})
            sess = SESSIONS.resolve(api.store, sid)
            if sess is None:
                return self._send_json({"error": {
                    "code": "unauthorized",
                    "message": "Session expired — please sign in again."}}, 401)
            return self._send_json({"user": user_view(sess, api.store)})
        # /api/games/{gid}/<sub>  — works for any game id, not just the seed.
        m = re.match(r"^/api/games/([^/]+)(?:/(board|lineups|roster-status|roster|substitutes|substitute-candidates|substitute-addable|reschedule|officials|availability-summary))?$", path)
        if m:
            gid, sub = m.group(1), m.group(2)
            # The bare game record is a public fixture (teams / time / rink /
            # score) — no player data, so it stays open (#73).
            if sub is None:
                return self._send_api(api.get_game(gid))
            # Everything else exposes player names, availability, roster
            # internals, or official assignments — never public. Require an
            # authenticated session (#73): in demo the headerless fallback is
            # the operator; a production anonymous request → 401.
            role, scope, user_id, err = self._resolve_role()
            if err is not None:
                code, payload = err
                return self._send_json(payload, code)
            # Authentication is not the whole gate: a signed-in viewer or an
            # unrelated coach/player/official must not read another game's
            # private data (#73 review). Operators see all; coach/player only
            # their team's games; official only games they're assigned to.
            if not can_read_private_game_data(role, scope, gid, api.store):
                return self._send_json({"error": {
                    "code": "forbidden",
                    "message": "You cannot view private data for this game.",
                    "details": {"role": role.value}}}, 403)
            if sub == "board":
                return self._send_api(api.get_board(gid))
            if sub == "lineups":
                return self._send_api(api.get_lineups(gid))
            if sub == "officials":
                return self._send_api({"officials": api.get_officials_for_game(gid)})
            if sub == "roster-status":
                return self._send_api(api.get_roster_status(gid))
            if sub == "roster":
                return self._send_api(api.get_roster(gid))
            if sub == "substitutes":
                return self._send_api(api.get_substitutes(gid))
            if sub == "reschedule":
                # A game's own reschedule-request history/current pending
                # request (#29) — same private-game-data gate as roster/
                # substitutes above, no further scoping: either team's coach,
                # any operator, or an assigned official may see it.
                return self._send_api(
                    {"requests": api.list_reschedule_requests(gid)})
            if sub == "availability-summary":
                # Availability rollup for a team (#89). The #73 gate above only
                # proves the caller belongs to *a* team in this game — it does
                # not bound *which* team's summary they may read. Add a
                # team-level scope check (#89 review): a coach/player may read
                # only their own team; operators (league admin / arena manager)
                # any team in the game. The team defaults to the caller's own
                # scope when not specified.
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                # Resolve the caller's own team authoritatively (#160): a Coach's
                # stored team_id, a Player's live team from player_id — never a
                # stale stored team_id — so the opponent-summary block holds for
                # a Player whose scope is player_id only.
                own_team = own_team_id(role, scope, api.store) or ""
                team_id = (qs.get("team_id") or [own_team])[0]
                if role in (Role.COACH, Role.PLAYER) and own_team \
                        and team_id != own_team:
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "You can only view your own team's "
                                   "availability."}}, 403)
                return self._send_api(
                    api.get_availability_summary(gid, team_id))
            if sub == "substitute-candidates":
                # Coach outreach queue (#112): manage_roster only — a player
                # must not see the operator candidate list even for their own
                # game. A coach is further bound to their own team; operators
                # (league admin / arena manager) may read any team's queue.
                from urllib.parse import parse_qs, urlparse
                if not can(role, Permission.MANAGE_ROSTER):
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "Only a coach or operator can view the "
                                   "substitute outreach queue."}}, 403)
                qs = parse_qs(urlparse(self.path).query)
                own_team = scope.get("team_id") or ""
                team_id = (qs.get("team_id") or [own_team])[0] or None
                if role == Role.COACH and own_team and team_id != own_team:
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "You can only view your own team's queue."}}, 403)
                return self._send_api(
                    api.get_substitute_candidates(gid, team_id))
            if sub == "substitute-addable":
                # Eligible-but-not-yet-enrolled team players a coach could add
                # as a substitute candidate (#114) — same manage_roster +
                # own-team gate as substitute-candidates above.
                from urllib.parse import parse_qs, urlparse
                if not can(role, Permission.MANAGE_ROSTER):
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "Only a coach or operator can view "
                                   "addable substitutes."}}, 403)
                qs = parse_qs(urlparse(self.path).query)
                own_team = scope.get("team_id") or ""
                team_id = (qs.get("team_id") or [own_team])[0] or None
                if role == Role.COACH and own_team and team_id != own_team:
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "You can only view your own team's roster."}}, 403)
                return self._send_api(
                    api.get_addable_substitutes(gid, team_id))
        if path.startswith("/api/"):
            return self._send_json({"error": {"code": "not_found",
                                              "message": "Unknown endpoint."}}, 404)
        return self._serve_static(path)

    def _supported_methods(self, path: str) -> set:
        """The HTTP methods a known path supports (#271).

        A GET-pattern match implies ``HEAD`` (the stdlib services HEAD from the
        same route); a POST-pattern match implies ``POST``. When any method is
        supported the path also answers ``OPTIONS``. An empty set means the path
        is unknown.
        """
        methods = set()
        if any(p.match(path) for p in _GET_ROUTES):
            methods.update({"GET", "HEAD"})
        if any(p.match(path) for p in _POST_ROUTES):
            methods.add("POST")
        if methods:
            methods.add("OPTIONS")
        return methods

    def _send_status(self, code: int, extra_headers=None) -> None:
        """Send a bodyless response (status line + standard headers, no body).

        Used for HEAD/OPTIONS and other empty responses (#271): a HEAD or a 204
        must never write a body, so this sends ``Content-Length: 0`` and ends
        the headers without a payload.
        """
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self._security_headers()
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()

    def _method_fallback(self, method: str) -> None:
        """Answer a known path hit with an unsupported method (#271).

        The stdlib returns an HTML 501 for any verb without a ``do_<METHOD>``
        handler. Instead: a known path → JSON 405 with a correct ``Allow``
        header (built from ``_supported_methods``, so it includes HEAD/OPTIONS
        where applicable); an unknown ``/api/...`` path → JSON 404; any other
        path → JSON 404.
        """
        path = self.path.split("?", 1)[0]
        methods = self._supported_methods(path)
        if methods:
            allow = ", ".join(sorted(methods))
            return self._send_json({"error": {
                "code": "method_not_allowed",
                "message": f"The {method} method is not allowed for {path}.",
                "details": {"allow": allow},
            }}, 405, extra_headers=[("Allow", allow)])
        return self._send_json({"error": {
            "code": "not_found", "message": "Unknown endpoint."}}, 404)

    def do_PUT(self):
        self._method_fallback("PUT")

    def do_PATCH(self):
        self._method_fallback("PATCH")

    def do_DELETE(self):
        self._method_fallback("DELETE")

    def do_HEAD(self):
        """HEAD contract (#271): a bodyless mirror of GET's method posture.

        A GET-supported path → 200 (+ ``Allow``); a POST-only path → 405
        (+ ``Allow``); an unknown path → 404 — always without a body.
        """
        path = self.path.split("?", 1)[0]
        methods = self._supported_methods(path)
        if not methods:
            return self._send_status(404)
        allow = ", ".join(sorted(methods))
        code = 200 if "GET" in methods else 405
        return self._send_status(code, [("Allow", allow)])

    def do_OPTIONS(self):
        """OPTIONS contract (#271): a known path → 204 (+ ``Allow``); an unknown
        path → 404. Always bodyless."""
        path = self.path.split("?", 1)[0]
        methods = self._supported_methods(path)
        if not methods:
            return self._send_status(404)
        return self._send_status(204, [("Allow", ", ".join(sorted(methods)))])

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        api = STATE.api
        try:
            body = self._read_json_object()
        except BodyError as exc:
            return self._send_json(exc.payload, exc.status)
        pid: Optional[str] = body.get("player_id")
        # There is deliberately no shared client-suppliable `actor`/`actor_id`
        # variable here (#136): every route below that writes to the audit
        # trail uses a server-resolved `user_id` from `_resolve_role()` —
        # either a route-local call some custom-guarded routes make before
        # the generic gate, or the one at the generic authorize() gate below,
        # which stays in scope for every route reached after it. A
        # body-supplied actor_id must never reach an audit entry.

        # -- authentication (#50/#67): login / logout are open (no role required) --
        if path == "/api/bootstrap/claim":
            # The only anonymous setup mutation (#174): fresh production,
            # durable store, configured one-time code, and zero prior claim.
            # Rate-limit before code comparison; neither submitted secret is
            # logged, echoed, persisted, or placed in the URL.
            if self._rate_limited("bootstrap_claim", limit=5, window_seconds=60):
                return
            try:
                account = claim_installation(
                    api, os.environ, _app_mode(),
                    body.get("setup_code", ""),
                    body.get("username", ""),
                    body.get("password", ""))
            except DomainError as exc:
                return self._send_api(exc.to_dict())
            token = SESSIONS.login(
                api.store, account.id,
                user_agent=self.headers.get("User-Agent"))
            sess = SESSIONS.resolve(api.store, token)
            cookie = self._session_cookie(token, SESSION_MAX_AGE)
            return self._send_json(
                {"user": user_view(sess, api.store)},
                extra_headers=[("Set-Cookie", cookie)])
        if path == "/api/auth/login":
            # Credentials are verified against a real, hashed UserAccount
            # (#67); role + scope come from that account, not a login-time
            # special case, so the session already carries "own team / own
            # self" binding for scope enforcement (#51).
            #
            # Login throttling (#267): a failed-attempt lockout keyed on BOTH the
            # source IP and the normalized username. The SAME generic response is
            # returned whether or not the username exists — a locked caller gets a
            # generic 429 + Retry-After, an unlocked bad credential the generic
            # 401 — so neither the throttle nor the error is a username oracle.
            #
            # ``begin`` reserves an in-flight slot atomically, so concurrent
            # attempts can't overshoot the ceiling between the check and the
            # record; the reservation is always released below via
            # record_failure / record_success / cancel.
            ip = self._client_ip()
            norm_username = (body.get("username", "") or "").strip().lower()
            wait = LOGIN_THROTTLE.begin(ip, norm_username)
            if wait > 0:
                return self._send_json(
                    {"error": {"code": "rate_limited",
                               "message": "Too many login attempts. Please wait "
                                          "and try again shortly."}},
                    429, extra_headers=[("Retry-After", str(int(wait) + 1))])
            try:
                row = api.verify_login(body.get("username", ""),
                                       body.get("password", ""))
            except Exception:  # release the reserved slot; never leak it on error
                LOGIN_THROTTLE.cancel(ip, norm_username)
                raise
            if row is None:
                newly_locked = LOGIN_THROTTLE.record_failure(ip, norm_username)
                if newly_locked:
                    # Best-effort, one-time safe audit when a bucket first locks —
                    # never let an audit hiccup turn a failed login into a 500.
                    try:
                        api.accounts.audit_login_throttled(ip, newly_locked)
                    except Exception:  # noqa: BLE001 — audit must never break login
                        pass
                return self._send_json({"error": {
                    "code": "unauthorized",
                    "message": "Invalid username or password."}}, 401)
            LOGIN_THROTTLE.record_success(ip, norm_username)
            token = SESSIONS.login(api.store, row["id"],
                                   user_agent=self.headers.get("User-Agent"))
            sess = SESSIONS.resolve(api.store, token)
            cookie = self._session_cookie(token, SESSION_MAX_AGE)
            return self._send_json({"user": user_view(sess, api.store)},
                                   extra_headers=[("Set-Cookie", cookie)])
        if path == "/api/auth/logout":
            SESSIONS.logout(api.store, self._cookie(SESSION_COOKIE))
            expire = self._session_cookie("", 0)
            return self._send_json({"ok": True},
                                   extra_headers=[("Set-Cookie", expire)])

        # Public calendar subscription (#33): anyone may mint a team or
        # division feed token, no session required — the resulting feed
        # carries exactly the same public-safe fixtures already served
        # unauthenticated at /api/public/schedule, so minting it needs no more
        # authority than reading that page. Deliberately excludes "player" and
        # "official": those feeds are gated to the owning account (or an
        # operator) via /api/calendar-feeds below, since a junior's schedule
        # is not public-safe to hand out from an anonymous portal (#26/#35 —
        # a junior's authority is scoped to a verified guardian link, not to
        # anyone who can reach a public page).
        #
        # Existence-oracle decision (#131, reviewed per #130 Phase 2): this
        # route 404s for a team/division id that doesn't exist, which is a
        # narrow existence oracle for a team that's been created but never
        # assigned to a division or published a game. Decided to KEEP the
        # 404 rather than switch to a generic denial: (a) team existence is
        # already public for any team actually reachable through normal use
        # — every division-assigned team appears in /api/public/standings'
        # rows, and every division id is already listed by
        # /api/public/schedule regardless of whether it has published games
        # — so the residual gap is only pre-onboarding/orphaned teams, which
        # carry no data beyond the bare fact of an id existing; (b) a
        # generic-denial response would degrade the legitimate error message
        # for the overwhelmingly common case (a genuine typo/stale link) to
        # protect a narrow, low-value edge case; (c) this route is already
        # rate-limited (below), which is the more relevant control against
        # enumeration than response-shape ambiguity.
        if path == "/api/public/calendar-feeds":
            # Tightest limit of the anonymous surface (#131): this is the
            # app's only anonymous route that writes to storage, so it gets
            # a real ceiling rather than the generous read-route allowance.
            if self._rate_limited("public_feed_mint", limit=5, window_seconds=60):
                return
            actor_type = body.get("actor_type")
            if actor_type not in ("team", "division"):
                return self._send_json({"error": {
                    "code": "validation_error",
                    "message": "Public calendar subscriptions are available "
                               "for a team or division only."}}, 400)
            return self._send_api(api.create_calendar_feed_token(
                actor_type, body.get("actor_ref"), label=body.get("label")))

        # Notification preferences (#81): custom access — an operator manages
        # anyone's, a signed-in user only their own — so it is guarded here
        # rather than by the role→permission gate below.
        if path == "/api/notifications/preferences":
            recipient_ref = body.get("recipient_ref")
            if self._prefs_guard(recipient_ref):
                return
            # Attribute the change to the signed-in user, not a client-supplied
            # actor_id, so the audit trail (#81) cannot be forged. The guard
            # above already established the session is valid.
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.set_notification_preference(
                recipient_ref, body.get("channel"), bool(body.get("enabled")),
                digest=body.get("digest"), actor_id=actor_uid))

        # Official availability (#88): an official manages their own windows, an
        # operator anyone's — guarded here rather than by the permission gate.
        oavs = re.match(r"^/api/officials/([^/]+)/availability$", path)
        if oavs:
            if self._official_guard(oavs.group(1)):
                return
            # Attribute the change to the signed-in user, not a client-supplied
            # actor_id, so the audit trail (#88) cannot be forged. The guard
            # above already established the session is valid.
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.set_official_availability(
                oavs.group(1), body.get("start_time"), body.get("end_time"),
                body.get("status"), note=body.get("note"), actor_id=actor_uid))
        oavd = re.match(r"^/api/officials/availability/([^/]+)/delete$", path)
        if oavd:
            avail = api.store.get_official_availability(oavd.group(1))
            if avail is None:
                return self._send_json({"error": {
                    "code": "not_found", "message": "Availability not found."}}, 404)
            if self._official_guard(avail.official_id):
                return
            # Server-resolved actor, not a forgeable body field (#88).
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.delete_official_availability(
                oavd.group(1), actor_id=actor_uid))

        # Reschedule opponent response (#29): the opponent team's coach OR
        # an operator (MANAGE_SCHEDULE — e.g. an Arena Manager, who holds no
        # MANAGE_ROSTER) may respond. That's richer than the single coarse
        # permission the generic authorize() gate checks, which would
        # otherwise reject an Arena Manager before _reschedule_opponent_guard
        # ever got a chance to allow the operator fallback it's designed to
        # grant — guarded here instead, mirroring official availability above.
        resp = re.match(r"^/api/games/[^/]+/reschedule/([^/]+)/respond$", path)
        if resp:
            if self._reschedule_opponent_guard(resp.group(1)):
                return
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.respond_to_reschedule(
                resp.group(1), bool(body.get("accept")), actor_uid))

        # Calendar feed tokens (#82): create / revoke. Custom access (operator
        # or own actor), guarded here rather than by the permission gate below.
        if path == "/api/calendar-feeds":
            if self._feed_guard(body.get("actor_type"), body.get("actor_ref")):
                return
            # Attribute the mint to the signed-in user (server-resolved), never
            # a client-supplied actor_id, so the audit trail (#82) is trusted.
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.create_calendar_feed_token(
                body.get("actor_type"), body.get("actor_ref"),
                label=body.get("label"), actor_id=actor_uid))
        cfr = re.match(r"^/api/calendar-feeds/([^/]+)/revoke$", path)
        if cfr:
            tok = api.store.get_calendar_feed_token(cfr.group(1))
            if tok is None:
                return self._send_json({"error": {
                    "code": "not_found", "message": "Feed token not found."}}, 404)
            if self._feed_guard(tok.actor_type, tok.actor_ref):
                return
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.revoke_calendar_feed_token(
                cfr.group(1), actor_id=actor_uid))

        # Player substitute-opportunity response (#110): signed-in-player
        # scoped, so the browser never passes a player_id — both the target
        # player and the audit actor come from the session. The verbs match the
        # services they call (enroll_substitute / withdraw_substitute), leaving
        # accept/decline free for the distinct coach-OFFER transition the rest
        # of the codebase already means by those words. No parallel workflow.
        mso = re.match(
            r"^/api/me/substitute-opportunities/([^/]+)/"
            r"(enroll|withdraw|accept-offer|decline-offer)$", path)
        if mso:
            ppid, uid, err = self._require_player_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            gid, action = mso.group(1), mso.group(2)
            # enroll/withdraw = the #110 self-service pool; accept-offer/
            # decline-offer = responding to a coach OFFER (#112). All four are
            # the same audited services, player identity from the session.
            if action == "enroll":
                return self._send_api(
                    api.enroll_substitute(gid, ppid, actor_id=uid))
            if action == "withdraw":
                return self._send_api(
                    api.withdraw_substitute(gid, ppid, actor_id=uid))
            if action == "accept-offer":
                return self._send_api(
                    api.accept_substitute(gid, ppid, actor_id=uid))
            return self._send_api(
                api.decline_substitute(gid, ppid, actor_id=uid))

        # Guardian acting for a linked junior (#26): a verified guardian may set
        # the junior's attendance and accept/decline coach offers on their
        # behalf. The subject player (jid) comes from the path; the acting
        # guardian (actor_id) comes from the session, so the audit trail records
        # WHO acted (the guardian) separately from WHOM it was for (the junior),
        # and `response_source="guardian"` marks the channel. Every action first
        # passes the verified-link gate, so an unlinked guardian gets 403.
        mga = re.match(
            r"^/api/me/guardian/([^/]+)/games/([^/]+)/availability$", path)
        if mga:
            guid, err = self._require_guardian_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            jid, gid = mga.group(1), mga.group(2)
            if self._guardian_link_or_403(guid, jid):
                return
            return self._send_api(api.set_availability(
                gid, jid, body.get("availability_status", "pending"),
                "guardian", guid))
        mgs = re.match(
            r"^/api/me/guardian/([^/]+)/substitute-opportunities/([^/]+)/"
            r"(accept-offer|decline-offer)$", path)
        if mgs:
            guid, err = self._require_guardian_scope()
            if err is not None:
                return self._send_json(err[1], err[0])
            jid, gid, action = mgs.group(1), mgs.group(2), mgs.group(3)
            if self._guardian_link_or_403(guid, jid):
                return
            if action == "accept-offer":
                return self._send_api(
                    api.accept_substitute(gid, jid, actor_id=guid))
            return self._send_api(
                api.decline_substitute(gid, jid, actor_id=guid))

        # Authorize the acting role at the HTTP boundary (#24/#50). A session
        # cookie is authoritative; the X-Demo-Role header is a dev fallback.
        role, scope, user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        if not authorize(role, path):
            perm = required_permission(path)
            return self._send_json({"error": {
                "code": "forbidden",
                "message": (f"Your role ({ROLE_LABELS[role]}) can't do this "
                            f"(requires {perm.value})."),
                "details": {"role": role.value,
                            "required": perm.value if perm else None},
            }}, 403)
        # Resource scoping (#51): a coach only their team, a player only self.
        # An unbound Coach fails closed (#266) — the ONLY permitted unscoped
        # coach is the demo-only X-Demo-Role dev fallback, which carries no
        # backing account (user_id is None) and is never read in production, so
        # the fallback stays explicit and impossible to activate there.
        allow_dev_fallback = user_id is None and _app_mode() != "production"
        violation = scope_violation(role, scope, path, body, api.store,
                                    allow_unscoped_dev_fallback=allow_dev_fallback)
        if violation is not None:
            return self._send_json({"error": {
                "code": "forbidden", "message": violation,
                "details": {"role": role.value, "scope": scope},
            }}, 403)

        if path in ("/api/reset", "/api/demo/reset",
                    "/api/demo/load", "/api/demo/clear"):
            # Demo lifecycle (#215), all MANAGE_SETUP-gated (League Admin only)
            # and unavailable in production:
            #   /api/demo/load  → build the canonical sample dataset (no confirm)
            #   /api/demo/reset → wipe + rebuild the sample dataset (type RESET)
            #   /api/demo/clear → wipe to a clean slate (type CLEAR)
            # /api/reset is a back-compat alias for reset.
            if _app_mode() == "production":
                return self._send_json({"error": {
                    "code": "demo_reset_not_available",
                    "message": "Demo controls are not available in this environment."}}, 403)
            if path == "/api/demo/load":
                seed, audit_action, confirm_word = True, "demo_loaded", None
            elif path == "/api/demo/clear":
                seed, audit_action, confirm_word = False, "demo_cleared", "CLEAR"
            else:
                seed, audit_action, confirm_word = True, "demo_reset", "RESET"
            if confirm_word and (body.get("confirm") or "").strip().upper() != confirm_word:
                return self._send_json({"error": {
                    "code": "validation_error",
                    "message": f"Type {confirm_word} to confirm."}}, 400)
            # Atomic (re)build, attributed to the authenticated session actor so
            # the new dataset carries a demo_loaded/demo_reset/demo_cleared audit
            # row. All-or-nothing (#215); on failure the previous dataset is left
            # intact and we return a deterministic error, not a half-built demo.
            try:
                STATE.reset(actor_id=user_id, seed=seed, audit_action=audit_action)
            except Exception:
                return self._send_json({"error": {
                    "code": "demo_reset_failed",
                    "message": ("The demo action could not be completed; the "
                                "previous data is unchanged.")}}, 500)
            # Reset rebuilds the store (dropping the sessions table), so the
            # operator's cookie is now dangling (#74). Demo accounts reseed
            # with deterministic ids, so re-issue a fresh session for the same
            # user and hand back the new cookie to keep them signed in.
            extra = None
            if user_id and STATE.api.store.get_user_account(user_id):
                token = SESSIONS.login(STATE.api.store, user_id,
                                       user_agent=self.headers.get("User-Agent"))
                extra = [("Set-Cookie",
                          self._session_cookie(token, SESSION_MAX_AGE))]
            return self._send_json({"ok": True}, extra_headers=extra)

        if path in ("/api/admin/factory-reset/preview",
                    "/api/admin/factory-reset/execute"):
            # Guarded production factory reset (#256): a whole-installation
            # wipe, deliberately separate from the demo lifecycle above (which
            # is itself unavailable in production) and from ordinary
            # per-record deletion. Unreachable unless BOTH this process is
            # actually running in production AND the deployment has opted in
            # via ALLOW_PRODUCTION_FACTORY_RESET — the flag alone is not
            # enough, since a demo deployment misconfigured with the flag set
            # should still never expose this. Every remaining safety control
            # (role/permission, password reauth, typed phrase, backup
            # acknowledgement, challenge validity, single-in-flight) is
            # enforced inside FactoryResetService itself, not here — the HTTP
            # layer only owns the flag gate, rate limiting, and request
            # parsing.
            if not (_app_mode() == "production" and _allow_production_factory_reset()):
                return self._send_json({"error": {
                    "code": "factory_reset_not_available",
                    "message": ("The production factory reset workflow is "
                               "not enabled for this deployment.")}}, 403)
            if self._rate_limited("factory_reset", limit=5, window_seconds=300):
                return
            if path == "/api/admin/factory-reset/preview":
                return self._send_api(api.factory_reset_preview(actor_id=user_id))
            environment = os.environ.get("DEPLOYMENT_ENV") or _app_mode()
            result = api.factory_reset_execute(
                actor_id=user_id,
                password=body.get("password"),
                typed_phrase=body.get("typed_phrase"),
                challenge_token=body.get("challenge_token"),
                # Passed through UNCOERCED (#256 review blocker 3): the
                # service requires the exact JSON boolean `true`, so
                # bool(...) here would wrongly accept "false"/"no"/1.
                backup_acknowledged=body.get("backup_acknowledged"),
                environment=environment)
            if isinstance(result, dict) and result.get("error"):
                return self._send_api(result)
            # Success (#256): the wipe already dropped every session server
            # side; also expire the caller's own cookie so the browser stops
            # sending a token for an account it can no longer resolve, and
            # require a fresh sign-in.
            expire = self._session_cookie("", 0)
            return self._send_json(result, extra_headers=[("Set-Cookie", expire)])

        # Quick action: add an available 90-min game slot on a rink for the
        # given date, after the latest existing slot on that rink that day.
        if path == "/api/demo/add-ice-slot":
            rink_id = body.get("rink_id") or STATE.ids.get("main_rink_id")
            if not rink_id:
                return self._send_api({"error": {"code": "validation_error",
                    "message": "A rink_id is required."}})
            date = body.get("date")  # "YYYY-MM-DD"
            ends = [s.end_time for s in api.store.ice_slots.values()
                    if s.rink_id == rink_id
                    and (not date or s.start_time.isoformat().startswith(date))]
            if ends:
                start = max(ends) + timedelta(minutes=30)
            elif date:
                start = datetime.fromisoformat(f"{date}T18:00:00+00:00")
            else:
                return self._send_api({"error": {"code": "validation_error",
                                                 "message": "No reference slot."}})
            end = start + timedelta(minutes=90)
            return self._send_api(api.create_ice_slot(
                rink_id, start.isoformat(), end.isoformat(),
                body.get("slot_type", "game"), actor_id="arena_mgr"))

        # Canonical v2 setup writes (#233 Slice C2). Same operator authz as v1
        # (enforced above), but canonical request bodies and canonical
        # (_serialize) responses — NO v1 adapter mapping. Checked before the v1
        # prefix so /api/v2/setup/ never falls through to /api/setup/.
        if path.startswith("/api/v2/setup/"):
            return self._handle_setup_v2(path[len("/api/v2/setup/"):], body, user_id)

        # Setup create endpoints — operator creates real records via the API.
        if path.startswith("/api/setup/"):
            return self._handle_setup(path[len("/api/setup/"):], body, user_id)

        # Pilot onboarding import dry-run (#92): validates CSV-shaped rows and
        # always returns 200 with the report itself (ok true/false) — nothing
        # is written. Only a malformed request (caught below) isn't 200.
        if path == "/api/import/dry-run":
            return self._send_api(api.get_import_dry_run(body))

        # Pilot onboarding import COMMIT — teams + players only (#93). The
        # path names the scope explicitly: officials/rinks/ice_slots commit
        # are separate endpoints in #94/#95, not a query param on this one.
        # Actually writes teams/players (and any find-or-created
        # clubs/divisions) after re-validating via the same gate as the
        # dry-run above. 200 either way (committed true/false is itself the
        # report); only a malformed request (bad season_id, an unsupported
        # sheet key) is a non-200 structured error.
        if path == "/api/import/commit/teams-players":
            # Server-resolved actor, not the forgeable body field (review):
            # this writes a whole tree of audit rows (club/division/team/
            # player/import_batch), so attribution must come from the
            # already-authenticated session, matching the notification-
            # preference/official-availability/calendar-feed precedent.
            return self._send_api(api.commit_teams_players_import(
                body.get("season_id"), body, actor_id=user_id))

        # Pilot onboarding import COMMIT — officials + availability (#94).
        # Server-resolved actor, not the forgeable body field — same fix
        # #93 had to make after review, applied here from the start.
        if path == "/api/import/commit/officials-availability":
            return self._send_api(api.commit_officials_availability_import(
                body, actor_id=user_id))

        # Pilot onboarding import COMMIT — rinks + ice slots (#95).
        # Server-resolved actor, not the forgeable body field — same fix
        # #93 had to make after review, applied here from the start.
        if path == "/api/import/commit/rinks-ice-slots":
            return self._send_api(api.commit_rinks_ice_slots_import(
                body, actor_id=user_id))

        # Season scheduler v1 (#84, extended #233 Slice G): generate a draft
        # round-robin proposal for a Division, or for a whole League within a
        # Season optionally narrowed to one Division. Returns a preview only —
        # nothing is created or published.
        if path == "/api/scheduler/draft":
            return self._send_api(api.draft_season_schedule(
                division_id=body.get("division_id"),
                season_id=body.get("season_id"), league_id=body.get("league_id"),
                slot_ids=body.get("slot_ids"),
                constraints=body.get("constraints")))
        # Draft review + publish (#86): commit a proposal to draft games, then
        # publish or discard them. All operator-only (MANAGE_SCHEDULE gate).
        # Attribute draft commit/publish/discard to the signed-in user resolved
        # server-side (#86 audit trail), never a client-supplied actor_id.
        if path == "/api/scheduler/commit":
            return self._send_api(api.commit_draft_schedule(
                division_id=body.get("division_id"),
                season_id=body.get("season_id"), league_id=body.get("league_id"),
                slot_ids=body.get("slot_ids"),
                constraints=body.get("constraints"), actor_id=user_id))
        if path == "/api/scheduler/drafts/publish":
            return self._send_api(api.publish_draft_games(
                game_ids=body.get("game_ids"), all_drafts=bool(body.get("all")),
                actor_id=user_id))
        if path == "/api/scheduler/drafts/discard":
            return self._send_api(api.discard_draft_games(
                game_ids=body.get("game_ids"), all_drafts=bool(body.get("all")),
                actor_id=user_id))

        # Notification delivery worker: drain the pending queue (#58).
        if path == "/api/notifications/deliveries/process":
            return self._send_api(api.process_notification_deliveries())

        # Dead-letter operations (#80): requeue or permanently ignore one row.
        dr = re.match(r"^/api/notifications/deliveries/([^/]+)/retry$", path)
        if dr:
            return self._send_api(api.retry_notification_delivery(dr.group(1)))
        di = re.match(r"^/api/notifications/deliveries/([^/]+)/ignore$", path)
        if di:
            return self._send_api(api.ignore_notification_delivery(di.group(1)))

        # Contact registry: register/update a real destination (#60). No
        # delete route — a contact destination is never erased, only
        # retired (#232 review 4): deleting one outright would erase the
        # integration history #232 requires be preserved, and a MISSING row
        # would fall back to the resolver's default-enabled placeholder,
        # which could silently redirect a still-pending delivery.
        if path == "/api/notifications/contacts":
            return self._send_api(api.set_contact_destination(
                body.get("recipient_ref"), body.get("channel"),
                body.get("destination"), body.get("label")))
        # Retire/reactivate (#232 review 4): the audited, non-destructive way
        # to clear a Player/Official delete's contact-destination
        # dependency — the row and its history are preserved, just no
        # longer counted as live.
        cda = re.match(r"^/api/notifications/contacts/([^/]+)/active$", path)
        if cda:
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.set_contact_destination_active(
                cda.group(1), bool(body.get("active")), actor_id=actor_uid))

        # Device token registry: register / activate-deactivate (#65).
        if path == "/api/notifications/device-tokens":
            return self._send_api(api.register_device_token(
                body.get("recipient_ref"), body.get("provider"),
                body.get("token"), body.get("label")))
        dt = re.match(r"^/api/notifications/device-tokens/([^/]+)/active$", path)
        if dt:
            return self._send_api(api.set_device_token_active(
                dt.group(1), bool(body.get("active"))))

        # Retire/reactivate a notification preference (#232 review 4) — the
        # audited, non-destructive counterpart to the contact-destination
        # route above, for the same Player/Official delete lifecycle.
        npa = re.match(r"^/api/notifications/preferences/([^/]+)/active$", path)
        if npa:
            _role, _scope, actor_uid, _err = self._resolve_role()
            return self._send_api(api.set_notification_preference_active(
                npa.group(1), bool(body.get("active")), actor_id=actor_uid))

        # User accounts: operator creates a login, or activates/deactivates
        # one (#67). No self-service signup — this is the only way an
        # account comes into existence besides the demo seed.
        if path == "/api/accounts":
            # Reject unknown top-level fields (#266/#271): the scope binding must
            # travel inside `scope` (e.g. {"scope": {"team_id": …}}). A misplaced
            # top-level `team_id` (or any stray key) would otherwise be silently
            # dropped, creating an unscoped Coach that looks assigned to the
            # operator — so fail loudly instead of quietly ignoring it. Required
            # fields are named here too, before the service call.
            try:
                check_body(body, allowed={"username", "password", "role",
                                          "scope"},
                           required=("username", "password", "role"),
                           types={"username": str, "password": str,
                                  "role": str})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            # Attribute the mint to the signed-in league admin (server-
            # resolved), never a client-supplied actor_id, so the audit
            # trail (#67) cannot be forged (#135).
            _role, _scope, user_id, _err = self._resolve_role()
            return self._send_api(api.create_user_account(
                body.get("username"), body.get("password"), body.get("role"),
                scope=body.get("scope"), actor_id=user_id))
        acc = re.match(r"^/api/accounts/([^/]+)/active$", path)
        if acc:
            res = api.set_user_account_active(acc.group(1), bool(body.get("active")))
            if isinstance(res, dict) and "error" not in res and not res.get("active"):
                # Deactivating an account must end any session it already has,
                # not just block future logins.
                SESSIONS.revoke_for_user(api.store, acc.group(1))
            return self._send_api(res)
        scp = re.match(r"^/api/accounts/([^/]+)/scope$", path)
        if scp:
            # Rebind an account's scope (#266 remediation): the audited actor is
            # the server-resolved signed-in admin, never a client body value.
            # The raw `scope` value is passed through UNCOERCED so the service
            # can reject a non-object shape as a stable 400.
            return self._send_api(api.rebind_user_account_scope(
                scp.group(1), body.get("scope"), actor_id=user_id))
        rv = re.match(r"^/api/accounts/([^/]+)/sessions/([^/]+)/revoke$", path)
        if rv:
            # Revoke one session (#78). Auto-guarded by the POST authorize()
            # gate above (MANAGE_USERS). The revoked session stops resolving
            # immediately — the store is authoritative. actor_id is the
            # signed-in admin's own user_id from the resolved session, NOT the
            # client-suppliable body actor_id — this action is audited, so the
            # acting identity must come from the server-verified session.
            return self._send_api(api.revoke_account_session(
                rv.group(1), rv.group(2), actor_id=user_id))

        # Guardian↔junior links: operator creates/verifies (#35). actor_id is
        # the signed-in admin's own user_id from the resolved session, NOT the
        # client-suppliable body actor_id — verification is a consent record,
        # so the acting identity must come from the server-verified session
        # (same reasoning as revoke_account_session above).
        if path == "/api/guardians/links":
            return self._send_api(api.create_guardian_link(
                body.get("guardian_user_id"), body.get("player_id"),
                actor_id=user_id))
        glv = re.match(r"^/api/guardians/links/([^/]+)/verify$", path)
        if glv:
            return self._send_api(api.verify_guardian_link(
                glv.group(1), body.get("consent_method"), actor_id=user_id))

        # Notifications feed: mark read / read-all (#32).
        if path == "/api/notifications/read-all":
            return self._send_api(api.mark_all_notifications_read(
                role.value, scope, user_id=user_id))
        nr = re.match(r"^/api/notifications/([^/]+)/read$", path)
        if nr:
            return self._send_api(api.mark_notification_read(
                nr.group(1), role.value, scope, user_id=user_id))

        # Official accepts/declines a proposed assignment, or it's unassigned (#30).
        oa = re.match(r"^/api/officials/assignments/([^/]+)/(accept|decline|unassign)$", path)
        if oa:
            # These verbs carry no body — the assignment id is in the path and
            # the actor is the server-resolved session (#271): reject any key so
            # a stray/typo'd field can't be silently ignored.
            try:
                check_body(body, allowed=set())
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            aid, op = oa.group(1), oa.group(2)
            if op == "unassign":
                return self._send_api(api.unassign_official(aid, user_id))
            return self._send_api(api.respond_assignment(aid, op == "accept", user_id))

        # /api/games/{gid}/<action>
        m = re.match(r"^/api/games/([^/]+)/(.+)$", path)
        if m:
            gid, action = m.group(1), m.group(2)
            if action == "availability":
                # Strict schema (#271): reject unknown keys so a typo'd status
                # field can't be silently recorded as the default `pending`, and
                # validate the status value against the domain enum before the
                # write so nothing is recorded for a bad value.
                try:
                    check_body(body,
                               allowed={"player_id", "availability_status",
                                        "response_source"},
                               types={"player_id": str,
                                      "availability_status": str,
                                      "response_source": str})
                except BodyError as exc:
                    return self._send_json(exc.payload, exc.status)
                status_val = body.get("availability_status", "pending")
                if status_val not in _AVAILABILITY_VALUES:
                    return self._send_json({"error": {
                        "code": "validation_error",
                        "message": (f"'{status_val}' is not a valid availability "
                                    "status."),
                        "details": {"reason": "invalid_value",
                                    "field": "availability_status"},
                    }}, 400)
                return self._send_api(api.set_availability(
                    gid, pid, status_val,
                    body.get("response_source", "player"), user_id))
            if action == "availability/remind":
                # One-click reminder to players who haven't responded (#89).
                return self._send_api(api.remind_unresponded(
                    gid, body.get("team_id") or scope.get("team_id"), user_id))
            if action == "build-roster":
                return self._send_api(api.auto_build_roster(
                    gid, body.get("team_id"), user_id))
            if action == "roster/select":
                return self._send_api(api.select_roster(
                    gid, body.get("player_ids", []), user_id))
            if action == "roster/remove":
                return self._send_api(api.remove_player(gid, pid, user_id))
            if action == "roster/copy-previous":
                return self._send_api(api.copy_previous_roster(
                    gid, body.get("team_id"), user_id))
            if action == "officials/assign":
                return self._send_api(api.assign_official(
                    gid, body.get("official_id"), body.get("role", "referee"),
                    user_id, override_unavailable=bool(body.get("override_unavailable"))))
            if action == "result":
                return self._send_api(api.record_result(
                    gid, body.get("home_score"), body.get("away_score"), user_id))
            if action == "result/approve":
                return self._send_api(api.approve_result(gid, user_id))
            if action == "publish":
                return self._send_api(api.publish_game(gid, user_id))
            if action == "move":
                return self._send_api(api.move_game(
                    gid, body.get("ice_slot_id"), body.get("reason", ""), user_id))
            if action == "reschedule/request":
                # actor_id is the signed-in coach's own user_id from the
                # resolved session, NOT the client-suppliable body actor_id —
                # every step of this workflow is an audited consent-adjacent
                # decision, so the acting identity must not be forgeable
                # (same reasoning as revoke_account_session / the #35
                # guardian-link routes).
                return self._send_api(api.request_reschedule(
                    gid, body.get("team_id"), body.get("reason", ""), user_id))
            # reschedule/{id}/respond is handled earlier, before the coarse
            # gate above (see the custom-guarded block near official
            # availability) — MANAGE_SCHEDULE-only operators need to reach it
            # too, which the single-permission check here can't express.
            resched = re.match(r"^reschedule/([^/]+)/decide$", action)
            if resched:
                return self._send_api(api.decide_reschedule(
                    resched.group(1), bool(body.get("approve")),
                    new_ice_slot_id=body.get("new_ice_slot_id"),
                    note=body.get("note"), actor_id=user_id))
            if action == "substitutes/enroll":
                return self._send_api(api.enroll_substitute(gid, pid, user_id))
            if action == "substitutes/withdraw":
                return self._send_api(api.withdraw_substitute(gid, pid, user_id))
            if action == "substitutes/add-candidate":
                # Coach/operator adds an arbitrary eligible team player
                # directly to the substitute pool (#114) — same enroll, a
                # different (coach-side) actor and permission than the
                # player's own self-service substitutes/enroll above.
                return self._send_api(
                    api.add_substitute_candidate(gid, pid, user_id))
            sub = re.match(r"^substitutes/([^/]+)/(offer|accept|decline|add-to-roster)$", action)
            if sub:
                player_id, op = sub.group(1), sub.group(2)
                if op == "offer":
                    return self._send_api(api.offer_substitute(
                        gid, player_id, user_id, expires_at=body.get("expires_at")))
                fn = {"accept": api.accept_substitute,
                      "decline": api.decline_substitute,
                      "add-to-roster": api.add_substitute_to_roster}[op]
                return self._send_api(fn(gid, player_id, user_id))
            coach = {"roster/lock": api.lock_roster,
                     "roster/unlock": api.unlock_roster,
                     "cancel": api.cancel_game}.get(action)
            if coach:
                return self._send_api(coach(gid, user_id))

        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown endpoint."}}, 404)

    def _handle_reassign(self, entity: str, record_id: str, target: str,
                         body: dict, actor_id: str):
        """Dispatch /api/setup/<entity>/<id>/assign-<target> (#166 PR D).

        Each moves ``record_id`` under a new parent id read from the body; the
        facade validates the target exists (or accepts null to unassign the
        nullable links) and audits the old→new ids. ``actor_id`` is the
        server-resolved session user (#136), never client-supplied.
        """
        api = STATE.api
        b = body
        combo = (entity, target)
        # Strict schema (#271): each real reassign reads exactly one target key.
        # Reject any unknown key BEFORE the service call so a misplaced field
        # can't be silently dropped (zero writes/audits on rejection).
        _V1_REASSIGN_KEYS = {
            ("league", "organization"): {"organization_id"},
            ("venue", "organization"): {"organization_id"},
            ("rink", "venue"): {"venue_id"},
            ("division", "level"): {"level_id"},
            ("team", "club"): {"club_id"},
            ("player", "team"): {"team_id"},
        }
        if combo in _V1_REASSIGN_KEYS:
            try:
                check_body(b, allowed=_V1_REASSIGN_KEYS[combo])
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
        # v1 boundary (#233 C1b): legacy request keys map straight onto the
        # canonical facade args (same id values), and canonical result dicts are
        # mapped back to the exact legacy v1 response keys via _v1.
        if combo == ("league", "organization"):
            return self._send_api(_v1.program_to_v1(api.assign_program_organization(
                record_id, b.get("organization_id") or None, actor_id)))
        if combo == ("venue", "organization"):
            return self._send_api(api.assign_venue_organization(
                record_id, b.get("organization_id") or None, actor_id))
        if combo == ("rink", "venue"):
            return self._send_api(api.assign_rink_venue(
                record_id, b.get("venue_id"), actor_id))
        if combo == ("division", "level"):
            return self._send_api(_v1.division_to_v1(api.assign_division_league(
                record_id, b.get("level_id") or None, actor_id)))
        if combo == ("team", "club"):
            return self._send_api(_v1.team_to_v1(api.assign_team_club(
                record_id, b.get("club_id") or None, actor_id)))
        # ("team", "division") reassignment removed (#180): a Team's seasonal
        # division is set through its SeasonTeamRegistration, not the legacy
        # Team.division_id.
        if combo == ("player", "team"):
            return self._send_api(api.assign_player_team(
                record_id, b.get("team_id"), actor_id))
        return self._send_json({"error": {
            "code": "not_found",
            "message": f"Unknown reassignment {entity}/assign-{target}."}}, 404)

    def _handle_setup(self, entity: str, body: dict, actor_id: str):
        """Dispatch /api/setup/<entity> to the matching facade create method.

        ``actor_id`` is always the caller's server-resolved session user id
        (#136) — never a client-supplied body field, so the setup audit
        trail can't be forged.
        """
        api = STATE.api
        b = body
        # Reassignment routes (#166 PR D): /api/setup/<entity>/<id>/assign-<parent>.
        # Move a record under a new parent, recording the old→new ids in audit.
        m = re.match(r"^(league|venue|rink|division|team|player)/([^/]+)/assign-(\w+)$", entity)
        if m:
            return self._handle_reassign(m.group(1), m.group(2), m.group(3), b, actor_id)
        # Season team registrations (#180): register a permanent league team for
        # a season, reassign its division for that season, or remove it from the
        # season. All League-Admin (MANAGE_SETUP) via the /api/setup/ authz
        # catch-all; the actor is the server-resolved session user.
        # v1 boundary (#233 C1b): a registration result carries the canonical
        # competition league_id, which the v1 API never exposed — drop it here
        # via _v1.registration_to_v1 so the v1 registration shape is unchanged.
        mr = re.match(r"^seasons/([^/]+)/team-registrations$", entity)
        if mr:
            # Strict schema (#271): reject unknown keys before the write.
            try:
                check_body(b, allowed={"team_id", "division_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(_v1.registration_to_v1(
                api.register_team_for_season(
                    mr.group(1), b.get("team_id"), b.get("division_id") or None,
                    actor_id)))
        ma = re.match(r"^season-team-registration/([^/]+)/assign-division$", entity)
        if ma:
            try:
                check_body(b, allowed={"division_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(_v1.registration_to_v1(
                api.assign_season_team_division(
                    ma.group(1), b.get("division_id") or None, actor_id)))
        mx = re.match(r"^season-team-registration/([^/]+)/remove$", entity)
        if mx:
            try:
                check_body(b, allowed=set())  # no body
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(_v1.registration_to_v1(
                api.unregister_team_from_season(mx.group(1), actor_id)))
        # Safe destructive deletion (#215): /api/setup/<entity>/<id>/delete.
        # League-Admin only via the /api/setup MANAGE_SETUP catch-all; the actor
        # is the server-resolved session user. Each facade method runs a
        # pre-write dependency gate and returns a structured has_dependencies
        # error (with a details breakdown) when blocked, writing nothing.
        md = re.match(
            r"^(organization|league|season|level|division|club|team|venue|rink"
            r"|ice-slot|game)/([^/]+)/delete$", entity)
        if md:
            # Delete routes take no body (#271): reject any key before the
            # dependency gate/write so a stray field can't be silently ignored.
            try:
                check_body(b, allowed=set())
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            kind = md.group(1)
            deleter = {
                "organization": api.delete_organization,
                "league": api.delete_program, "season": api.delete_season,
                "level": api.delete_league, "division": api.delete_division,
                "club": api.delete_club, "team": api.delete_team,
                "venue": api.delete_venue, "rink": api.delete_rink,
                "ice-slot": api.delete_ice_slot, "game": api.delete_game,
            }[kind]
            # v1 boundary (#233 C1b): the deleted record is returned serialized,
            # so canonical entities are mapped back to their legacy v1 shape.
            _to_v1 = {"league": _v1.program_to_v1, "season": _v1.season_to_v1,
                      "division": _v1.division_to_v1, "team": _v1.team_to_v1,
                      "game": _v1.game_to_v1}
            mapper = _to_v1.get(kind, lambda r: r)
            return self._send_api(mapper(deleter(md.group(2), actor_id)))
        # Player/Official delete moved to v2 (#232/#271): the v1 delete regex
        # above deliberately excludes them, so the old v1 path would otherwise
        # fall through to a bare "Unknown setup entity" 404. Return an explicit
        # moved_to_v2 (409) pointing at the canonical v2 route, which already
        # supports player/official delete.
        mmv = re.match(r"^(player|official)/([^/]+)/delete$", entity)
        if mmv:
            # This delete route takes no body either (#271) — reject stray keys.
            try:
                check_body(b, allowed=set())
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            kind, rec = mmv.group(1), mmv.group(2)
            route = f"/api/v2/setup/{kind}/{rec}/delete"
            return self._send_json({"error": {
                "code": "moved_to_v2",
                "message": (f"{kind.capitalize()} delete has moved to v2. "
                            f"Use POST {route} instead."),
                "details": {"reason": "moved_to_v2", "entity": kind,
                            "route": route},
            }}, 409)
        # Season rollover (#180): copy a prior season's participation forward
        # into this one, reusing the permanent teams. body carries the source
        # season and an optional per-team target-division selection.
        mrf = re.match(r"^seasons/([^/]+)/roll-forward$", entity)
        if mrf:
            rolled = api.roll_forward_registrations(
                b.get("from_season_id"), mrf.group(1), b.get("selections"),
                actor_id)
            # v1 boundary (#233 C1b): each rolled-forward registration carries the
            # canonical competition league_id — drop it from every one so the v1
            # registration shape is unchanged. An error envelope passes through.
            if isinstance(rolled, dict) and "registrations" in rolled:
                rolled = {**rolled, "registrations": [
                    _v1.registration_to_v1(r) for r in rolled["registrations"]]}
            return self._send_api(rolled)
        # v1 boundary (#233 C1b): legacy request keys (organization_id, season
        # league_id, division level_id, team league_id) are read here and passed
        # to the canonical facade; canonical results are mapped back to legacy v1
        # response keys via _v1 so the v1 contract keeps the same JSON
        # keys/shape and values.
        if entity == "league":
            return self._send_api(_v1.program_to_v1(api.create_program(
                b.get("name"), b.get("country", ""), b.get("timezone", "UTC"),
                b.get("organization_id") or None, actor_id)))
        if entity == "season":
            return self._send_api(_v1.season_to_v1(api.create_season(
                b.get("league_id"), b.get("name"),
                b.get("start_date"), b.get("end_date"), actor_id)))
        if entity == "level":
            try:
                sort_order = int(b.get("sort_order") or 0)
            except (TypeError, ValueError):
                sort_order = 0
            return self._send_api(api.create_league(
                b.get("season_id"), b.get("name"), sort_order, actor_id))
        if entity == "division":
            return self._send_api(_v1.division_to_v1(api.create_division(
                b.get("season_id"), b.get("name"), b.get("age_group", ""),
                b.get("level_id") or None, actor_id)))
        if entity == "club":
            return self._send_api(api.create_club(
                b.get("name"), b.get("country", ""), actor_id))
        if entity == "team":
            # #180/#233: a team is created under a program (v1 body key
            # league_id); division_id is the legacy/import path that derives it.
            return self._send_api(_v1.team_to_v1(api.create_team(
                b.get("club_id"), b.get("division_id") or None, b.get("name"),
                actor_id, program_id=b.get("league_id") or None)))
        if entity == "organization":
            return self._send_api(api.create_organization(
                b.get("name"), b.get("short_name", ""), actor_id))
        if entity == "venue":
            return self._send_api(api.create_venue(
                b.get("name"), b.get("address", ""), b.get("timezone", "UTC"),
                b.get("organization_id") or None, b.get("league_id") or None, actor_id))
        if entity == "rink":
            return self._send_api(api.create_rink(
                b.get("venue_id"), b.get("name"), actor_id))
        if entity == "ice-slot":
            return self._send_api(api.create_ice_slot(
                b.get("rink_id"), b.get("start_time"), b.get("end_time"),
                b.get("slot_type", "game"), actor_id))
        if entity == "game":
            # v1 boundary (#233 C1b): a game result carries the canonical
            # competition league_id, which the v1 API never exposed — drop it via
            # _v1.game_to_v1 so the v1 game shape is unchanged.
            return self._send_api(_v1.game_to_v1(api.create_game(
                b.get("season_id"), b.get("division_id"), b.get("home_team_id"),
                b.get("away_team_id"), b.get("ice_slot_id"),
                allow_division_override=bool(b.get("allow_division_override")),
                actor_id=actor_id)))
        if entity == "official":
            return self._send_api(api.create_official(
                b.get("name"), b.get("home_club_id"), actor_id))
        if entity == "player":
            # Strict schema (#271): name the missing required field (team_id /
            # name / position), reject unknown keys, and type-check each field
            # (a boolean jersey_number is rejected) before the service call, so
            # a missing team_id becomes a `field_required` validation_error
            # rather than the old `NotFoundError("Team None not found.")`.
            try:
                check_body(b, allowed={"team_id", "name", "position",
                                       "jersey_number", "email"},
                           required=("team_id", "name", "position"),
                           types={"team_id": str, "name": str, "position": str,
                                  "jersey_number": int, "email": str})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.create_player(
                b.get("team_id"), b.get("name"), b.get("position"),
                jersey_number=b.get("jersey_number"), email=b.get("email"),
                actor_id=actor_id))
        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown setup entity."}}, 404)

    def _handle_reassign_v2(self, entity: str, record_id: str, target: str,
                            body: dict, actor_id: str):
        """Dispatch /api/v2/setup/<entity>/<id>/assign-<target> (#233 Slice C2).

        Canonical reassign per the ADR 0001 new tree: division→league (the
        grouping League), team→club, team→league (the PERMANENT competition
        League, #283 Slice B), player→team, rink→venue, venue→organization.
        There is NO team→program (a Team is program-permanent) and NO
        division→level (division's parent is now a League). Canonical request
        keys and canonical (_serialize) responses — no v1 mapping. ``actor_id``
        is the server-resolved session user (#136), never client-supplied.
        """
        api = STATE.api
        b = body
        combo = (entity, target)
        # Strict schema (#271): each real reassign reads exactly one target key
        # (the v2 key set differs from v1). Reject unknown keys before the
        # service call — zero writes/audits on rejection.
        _V2_REASSIGN_KEYS = {
            ("program", "organization"): {"operator_organization_id"},
            ("division", "league"): {"league_id"},
            ("team", "club"): {"club_id"},
            ("team", "league"): {"league_id"},
            ("player", "team"): {"team_id"},
            ("rink", "venue"): {"venue_id"},
            ("venue", "organization"): {"organization_id"},
        }
        if combo in _V2_REASSIGN_KEYS:
            try:
                check_body(b, allowed=_V2_REASSIGN_KEYS[combo])
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
        if combo == ("program", "organization"):
            # v2 Program operator reassignment (#233 Slice C2 review): the
            # canonical umbrella owner move, using ``operator_organization_id``.
            # The facade preserves the temporary Venue-link safety rule (the
            # operator can't change while venues are still attached).
            return self._send_api(api.assign_program_organization(
                record_id, b.get("operator_organization_id") or None, actor_id))
        if combo == ("division", "league"):
            # v2 Division reparent: League REQUIRED + dependent-record integrity.
            return self._send_api(api.assign_division_league(
                record_id, b.get("league_id") or None, actor_id, v2=True))
        if combo == ("team", "club"):
            # Club is optional in both v1 and v2 (#233 Slice D): a null
            # club_id unassigns the team's Club.
            return self._send_api(api.assign_team_club(
                record_id, b.get("club_id") or None, actor_id))
        if combo == ("team", "league"):
            # #283 Slice B: move a Team to a different PERMANENT League
            # (promotion/relegation/transfer, rule 10). League is required —
            # a Team is always league-permanent; history is preserved.
            if not (b.get("league_id") or None):
                return self._send_api({"error": {"code": "validation_error",
                    "message": "A league_id is required."}})
            return self._send_api(api.transfer_team_to_league(
                record_id, b.get("league_id"), actor_id))
        if combo == ("player", "team"):
            return self._send_api(api.assign_player_team(
                record_id, b.get("team_id"), actor_id))
        if combo == ("rink", "venue"):
            return self._send_api(api.assign_rink_venue(
                record_id, b.get("venue_id"), actor_id))
        if combo == ("venue", "organization"):
            # Canonical Venue is org-owned only — strip the legacy league_id.
            return self._send_api(_v2p.venue_to_v2(api.assign_venue_organization(
                record_id, b.get("organization_id") or None, actor_id)))
        return self._send_json({"error": {
            "code": "not_found",
            "message": f"Unknown reassignment {entity}/assign-{target}."}}, 404)

    def _handle_setup_v2(self, entity: str, body: dict, actor_id: str):
        """Dispatch /api/v2/setup/<entity> to the canonical facade (#233 Slice C2).

        The canonical write surface: canonical body keys (program_id /
        operator_organization_id / competition league_id) in, canonical
        ``_serialize`` output out — no ``v1_setup_adapter`` mapping. Canonical
        validation is REQUIRED on the v2 routes where the target model demands it
        (League on division / registration / game); a missing one is a
        validation_error rather than a silent v1-style derivation. ``actor_id`` is
        always the server-resolved session user (#136).
        """
        api = STATE.api
        b = body

        def _required_error(message):
            """Send a structured validation_error and return True (so the caller
            returns immediately). Returns False when nothing is sent."""
            self._send_api({"error": {
                "code": "validation_error", "message": message}})
            return True

        # Reassignment: /api/v2/setup/<entity>/<id>/assign-<parent>.
        m = re.match(
            r"^(program|division|team|player|rink|venue)/([^/]+)/assign-(\w+)$",
            entity)
        if m:
            return self._handle_reassign_v2(
                m.group(1), m.group(2), m.group(3), b, actor_id)
        # Season team registrations (canonical): league_id REQUIRED, division
        # optional. The result keeps its competition league_id (canonical).
        mr = re.match(r"^seasons/([^/]+)/team-registrations$", entity)
        if mr:
            # Strict schema (#271): reject unknown keys before the write.
            try:
                check_body(b, allowed={"team_id", "division_id", "league_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            if not (b.get("league_id") or None):
                return self._send_api({"error": {"code": "validation_error",
                    "message": "A league_id is required."}})
            return self._send_api(api.register_team_for_season(
                mr.group(1), b.get("team_id"), b.get("division_id") or None,
                actor_id, league_id=b.get("league_id") or None))
        ml = re.match(r"^season-team-registration/([^/]+)/assign-league$", entity)
        if ml:
            try:
                check_body(b, allowed={"league_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.assign_season_team_league(
                ml.group(1), b.get("league_id") or None, actor_id))
        ma = re.match(r"^season-team-registration/([^/]+)/assign-division$", entity)
        if ma:
            # v2: clearing the Division PRESERVES the required league_id; a set
            # Division must match the registration's League.
            try:
                check_body(b, allowed={"division_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.assign_season_team_division(
                ma.group(1), b.get("division_id") or None, actor_id, v2=True))
        mx = re.match(r"^season-team-registration/([^/]+)/remove$", entity)
        if mx:
            try:
                check_body(b, allowed=set())  # no body
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.unregister_team_from_season(
                mx.group(1), actor_id))
        # Explicit permanent cleanup of an already-inactive, game-free
        # registration (#251). Distinct from "remove" above, which only
        # deactivates and preserves the row for history.
        mdr = re.match(
            r"^season-team-registration/([^/]+)/delete$", entity)
        if mdr:
            try:
                check_body(b, allowed=set())  # no body
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.delete_season_team_registration(
                mdr.group(1), actor_id))
        # Season-venue access (#233 Slice E, E1): grant/revoke which Venues a
        # Season may use, independent of any Venue-Program ownership. Not a
        # v1 route — this is a new v2-only feature, no legacy shape to adapt.
        mva = re.match(r"^seasons/([^/]+)/venue-access$", entity)
        if mva:
            try:
                check_body(b, allowed={"venue_id"})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            if not (b.get("venue_id") or None):
                return self._send_api({"error": {"code": "validation_error",
                    "message": "A venue_id is required."}})
            return self._send_api(api.grant_season_venue_access(
                mva.group(1), b.get("venue_id"), actor_id))
        mvr = re.match(r"^season-venue-access/([^/]+)/remove$", entity)
        if mvr:
            try:
                check_body(b, allowed=set())  # no body
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.revoke_season_venue_access(
                mvr.group(1), actor_id))
        # Explicit permanent cleanup of an already-revoked access row (#255
        # review), mirroring #251's season-team-registration .../delete.
        mvd = re.match(r"^season-venue-access/([^/]+)/delete$", entity)
        if mvd:
            try:
                check_body(b, allowed=set())  # no body
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.delete_season_venue_access(
                mvd.group(1), actor_id))
        # Season rollover (canonical v2): each selection REQUIRES a target
        # league_id, honored verbatim on the resulting registration.
        mrf = re.match(r"^seasons/([^/]+)/roll-forward$", entity)
        if mrf:
            return self._send_api(api.roll_forward_registrations_v2(
                b.get("from_season_id"), mrf.group(1), b.get("selections"),
                actor_id))
        # Delete: /api/v2/setup/<entity>/<id>/delete — canonical names
        # (program-delete = umbrella, league-delete = the grouping League).
        md = re.match(
            r"^(organization|program|season|league|division|club|team|venue|rink"
            r"|ice-slot|game|official|player)/([^/]+)/delete$", entity)
        if md:
            # Delete routes take no body (#271): reject any key before the write.
            try:
                check_body(b, allowed=set())
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            kind = md.group(1)
            deleter = {
                "organization": api.delete_organization,
                "program": api.delete_program, "season": api.delete_season,
                "league": api.delete_league, "division": api.delete_division,
                "club": api.delete_club, "team": api.delete_team,
                "venue": api.delete_venue, "rink": api.delete_rink,
                "ice-slot": api.delete_ice_slot, "game": api.delete_game,
                "official": api.delete_official, "player": api.delete_player,
            }[kind]
            # Canonical Venue responses drop the legacy league_id (org-owned only).
            mapper = _v2p.venue_to_v2 if kind == "venue" else (lambda r: r)
            return self._send_api(mapper(deleter(md.group(2), actor_id)))

        # Entity create — canonical bodies, canonical responses.
        if entity == "program":
            return self._send_api(api.create_program(
                b.get("name"), b.get("country", ""), b.get("timezone", "UTC"),
                b.get("operator_organization_id") or None, actor_id))
        if entity == "season":
            return self._send_api(api.create_season(
                b.get("program_id"), b.get("name"),
                b.get("start_date"), b.get("end_date"), actor_id))
        if entity == "league":
            try:
                sort_order = int(b.get("sort_order") or 0)
            except (TypeError, ValueError):
                sort_order = 0
            return self._send_api(api.create_league(
                b.get("season_id"), b.get("name"), sort_order, actor_id))
        if entity == "division":
            # v2: parented by a League (REQUIRED); season derived from it.
            if not (b.get("league_id") or None):
                return _required_error("A league_id is required.")
            return self._send_api(api.create_division_v2(
                b.get("league_id"), b.get("name"), b.get("age_group", ""),
                actor_id))
        if entity == "club":
            return self._send_api(api.create_club(
                b.get("name"), b.get("country", ""), actor_id))
        if entity == "team":
            # v2 canonical (#283 Slice E): a Team is created under its PERMANENT
            # League (league_id REQUIRED); the service derives Program from it.
            # teams_without_league is only a legacy/migration remediation state —
            # the canonical create path never mints a new league-less Team.
            if not (b.get("league_id") or None):
                return _required_error("A league_id is required.")
            return self._send_api(api.create_team(
                b.get("club_id") or None, None, b.get("name"),
                actor_id, program_id=b.get("program_id") or None,
                league_id=b.get("league_id") or None))
        if entity == "organization":
            return self._send_api(api.create_organization(
                b.get("name"), b.get("short_name", ""), actor_id))
        if entity == "venue":
            # v2 omits league_id — venue↔program is a legacy v1-only relation
            # (Slice E decouples it); the v2 route never accepts it in the request
            # and strips it from the response (canonical Venue is org-owned only).
            return self._send_api(_v2p.venue_to_v2(api.create_venue(
                b.get("name"), b.get("address", ""), b.get("timezone", "UTC"),
                b.get("organization_id") or None, None, actor_id)))
        if entity == "rink":
            return self._send_api(api.create_rink(
                b.get("venue_id"), b.get("name"), actor_id))
        if entity == "ice-slot":
            return self._send_api(api.create_ice_slot(
                b.get("rink_id"), b.get("start_time"), b.get("end_time"),
                b.get("slot_type", "game"), actor_id))
        if entity == "game":
            # #283 Slice D: an EXHIBITION game is a cross-League-allowed friendly
            # with no owning League — so league_id is NOT required for it (and is
            # ignored). A REGULAR game keeps the v2 rule: league_id REQUIRED,
            # division_id optional.
            game_type = (b.get("game_type") or "regular")
            if game_type == "regular" and not (b.get("league_id") or None):
                return _required_error("A league_id is required.")
            return self._send_api(api.create_game(
                b.get("season_id"), b.get("division_id") or None,
                b.get("home_team_id"), b.get("away_team_id"),
                b.get("ice_slot_id"),
                allow_division_override=bool(b.get("allow_division_override")),
                actor_id=actor_id, league_id=b.get("league_id") or None,
                game_type=game_type))
        if entity == "official":
            return self._send_api(api.create_official(
                b.get("name"), b.get("home_club_id"), actor_id))
        if entity == "player":
            # Strict schema (#271), same as the v1 route: required fields named,
            # unknown keys rejected, fields type-checked (bool jersey_number
            # rejected), before the service call.
            try:
                check_body(b, allowed={"team_id", "name", "position",
                                       "jersey_number", "email"},
                           required=("team_id", "name", "position"),
                           types={"team_id": str, "name": str, "position": str,
                                  "jersey_number": int, "email": str})
            except BodyError as exc:
                return self._send_json(exc.payload, exc.status)
            return self._send_api(api.create_player(
                b.get("team_id"), b.get("name"), b.get("position"),
                jersey_number=b.get("jersey_number"), email=b.get("email"),
                actor_id=actor_id))
        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown setup entity."}}, 404)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    # Configure the delivery worker loop from env and start it if enabled (#79).
    # ApiService keeps a disabled loop by default (safe for tests); only the
    # running server process reads env and may spin the background thread.
    STATE.api.delivery_loop = delivery_loop_from_env(STATE.api.delivery,
                                                     os.environ)
    if STATE.api.delivery_loop.start():
        print(f"Delivery worker loop started "
              f"(every {STATE.api.delivery_loop.interval_seconds}s, "
              f"batch {STATE.api.delivery_loop.batch_size}).")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Hockey Scheduler demo running at http://{host}:{port}")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        STATE.api.delivery_loop.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hockey Scheduler demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)
