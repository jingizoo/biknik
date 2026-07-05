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
from ..bootstrap import bootstrap_admin_from_env
from ..domain import ROLE_LABELS, Permission, Role, can, permissions_for
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
from .scope import can_read_private_game_data, scope_violation

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

# Sessions live outside DemoState so signing in survives a demo-data reset.
SESSIONS = SessionManager()

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
}


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class DemoState:
    """Holds the seeded game + facade; can reset itself for the demo."""

    def __init__(self) -> None:
        self.reset()

    def _make_api(self, store):
        # Email/push transports come from EMAIL_MODE / SMTP_* (#63) and
        # PUSH_MODE / PUSH_* (#64) env; both dry-run by default, so nothing
        # sends real mail or push unless explicitly wired.
        return ApiService(
            store, email_transport=email_transport_from_env(os.environ),
            push_transport=push_transport_from_env(os.environ))

    def reset(self) -> None:
        store = create_store()  # SqlStore.__init__ applies pending numbered migrations (#75)

        # Production (#71): NEVER reset the schema or seed demo data — that
        # would wipe a persistent DATABASE_URL store on every boot. Preserve
        # whatever is there and only bootstrap the first admin from env if the
        # store has no accounts yet.
        if _app_mode() == "production":
            self.api = self._make_api(store)
            self.game_id = None
            self.ids = {}
            bootstrap_admin_from_env(self.api, os.environ)
            return

        # Demo mode: rebuild the full Alpine league/arena scenario via the real
        # setup service (one game rostered & confirmed, ready to demo the
        # back-out → substitute flow), reseeding a clean dataset each reset.
        if isinstance(store, SqlStore):
            store.reset_schema()
        store, game_id, ids = build_full_demo_store(store)
        self.api = self._make_api(store)
        self.game_id = game_id
        self.ids = ids
        self._seed_demo_accounts(ids)

    def _seed_demo_accounts(self, ids: dict) -> None:
        """Create the six demo personas as real, operator-created accounts
        (#67) — deterministic ids so existing sessions survive a reset (the
        session cookie carries role/scope already, so this only matters for
        display and for `/api/accounts` listings).
        """
        scopes = {
            "coach": {"team_id": ids.get("home_team_id")},
            "player": {"team_id": ids.get("home_team_id"),
                      "player_id": ids.get("selected_player_id")},
            "official": {"official_id": ids.get("referee_id")},
        }
        for username, role in DEMO_USERS.items():
            self.api.accounts.create_account(
                username, DEMO_PASSWORD, role, scope=scopes.get(username, {}),
                actor_id="demo_seed", account_id=f"user_{username}")


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

    @staticmethod
    def _own_recipient_ref(role, scope):
        """The delivery recipient_ref the signed-in user speaks for (#81), used
        to let a non-operator manage only their own preferences.

        ``team:<id>`` is a single shared delivery target (the COACH audience) —
        one preference row governs every notification sent to that team. Only a
        coach speaks for it. A PLAYER must NOT map here: notification fan-out in
        this slice never targets an individual player, so a player has no own
        delivery channel to configure, and letting them edit ``team:<id>`` would
        let one player mute the whole team's notifications."""
        if role == Role.OFFICIAL and scope.get("official_id"):
            return "official:" + scope["official_id"]
        if role == Role.COACH and scope.get("team_id"):
            return "team:" + scope["team_id"]
        return None

    def _prefs_guard(self, recipient_ref) -> bool:
        """Send 401/403 and return True if the caller may not manage
        ``recipient_ref``'s notification preferences (#81). An operator
        (MANAGE_SCHEDULE) may manage anyone; anyone else only their own."""
        role, scope, _uid, err = self._resolve_role()
        if err is not None:
            code, payload = err
            self._send_json(payload, code)
            return True
        if can(role, Permission.MANAGE_SCHEDULE):
            return False
        if recipient_ref and recipient_ref == self._own_recipient_ref(role, scope):
            return False
        self._send_json({"error": {
            "code": "forbidden",
            "message": "You can only manage your own notification preferences."}},
            403)
        return True

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            rel = "index.html"            # desktop web console
        elif path in ("/mobile", "/mobile/"):
            rel = "mobile.html"           # iPhone-framed preview
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
        # Public iCal feed (#82): bearer token in the URL scopes access; no
        # session. Unknown/revoked/mismatched token → 404 (don't confirm which).
        cal = re.match(r"^/calendar/(team|official|player)/([^/]+)\.ics$", path)
        if cal:
            ics = api.calendar_feed_ics(cal.group(1), cal.group(2))
            if ics is None:
                return self._send_json({"error": {
                    "code": "not_found", "message": "Calendar not found."}}, 404)
            return self._send_ics(ics)
        if path == "/api/demo/overview":
            return self._send_api(api.get_demo_overview())
        if path == "/api/status":
            # Deployment posture for the UI status chips (#72): app mode +
            # store backend + email/push delivery modes. Non-sensitive, no
            # auth — like a health endpoint.
            status = api.runtime_status()
            status["app_mode"] = _app_mode()
            return self._send_json(status)
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
        if path == "/api/officials":
            return self._send_api({"officials": api.get_officials()})
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
        sd = re.match(r"^/api/standings/([^/]+)$", path)
        if sd:
            return self._send_api(api.get_standings(sd.group(1)))
        # Public, no-auth surface (#83): schedule / standings / game detail,
        # public-safe fields only. Reachable unauthenticated in production.
        if path == "/api/public/schedule":
            return self._send_api(api.get_public_schedule())
        ps = re.match(r"^/api/public/standings/([^/]+)$", path)
        if ps:
            return self._send_api(api.get_public_standings(ps.group(1)))
        pg = re.match(r"^/api/public/games/([^/]+)$", path)
        if pg:
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
        m = re.match(r"^/api/games/([^/]+)(?:/(board|lineups|roster-status|roster|substitutes|officials|availability-summary))?$", path)
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
                own_team = scope.get("team_id") or ""
                team_id = (qs.get("team_id") or [own_team])[0]
                if role in (Role.COACH, Role.PLAYER) and own_team \
                        and team_id != own_team:
                    return self._send_json({"error": {
                        "code": "forbidden",
                        "message": "You can only view your own team's "
                                   "availability."}}, 403)
                return self._send_api(
                    api.get_availability_summary(gid, team_id))
        if path.startswith("/api/"):
            return self._send_json({"error": {"code": "not_found",
                                              "message": "Unknown endpoint."}}, 404)
        return self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        api = STATE.api
        body = self._read_body()
        pid: Optional[str] = body.get("player_id")
        actor = body.get("actor_id", "demo")

        # -- authentication (#50/#67): login / logout are open (no role required) --
        if path == "/api/auth/login":
            # Credentials are verified against a real, hashed UserAccount
            # (#67); role + scope come from that account, not a login-time
            # special case, so the session already carries "own team / own
            # self" binding for scope enforcement (#51).
            row = api.verify_login(body.get("username", ""),
                                   body.get("password", ""))
            if row is None:
                return self._send_json({"error": {
                    "code": "unauthorized",
                    "message": "Invalid username or password."}}, 401)
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
        violation = scope_violation(role, scope, path, body, api.store)
        if violation is not None:
            return self._send_json({"error": {
                "code": "forbidden", "message": violation,
                "details": {"role": role.value, "scope": scope},
            }}, 403)

        if path == "/api/reset":
            # Wiping and reseeding all data has no legitimate use once an app
            # is live — disabled outright in production (#68), not just
            # permission-gated, regardless of the caller's role.
            if _app_mode() == "production":
                return self._send_json({"error": {
                    "code": "forbidden",
                    "message": "Reset is disabled in production."}}, 403)
            STATE.reset()
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

        # Setup create endpoints — operator creates real records via the API.
        if path.startswith("/api/setup/"):
            return self._handle_setup(path[len("/api/setup/"):], body, actor)

        # Season scheduler v1 (#84): generate a draft round-robin proposal for a
        # division. Returns a preview only — nothing is created or published.
        if path == "/api/scheduler/draft":
            return self._send_api(api.draft_season_schedule(
                body.get("division_id"), slot_ids=body.get("slot_ids"),
                constraints=body.get("constraints")))
        # Draft review + publish (#86): commit a proposal to draft games, then
        # publish or discard them. All operator-only (MANAGE_SCHEDULE gate).
        # Attribute draft commit/publish/discard to the signed-in user resolved
        # server-side (#86 audit trail), never a client-supplied actor_id.
        if path == "/api/scheduler/commit":
            return self._send_api(api.commit_draft_schedule(
                body.get("division_id"), slot_ids=body.get("slot_ids"),
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

        # Contact registry: register/update a real destination (#60).
        if path == "/api/notifications/contacts":
            return self._send_api(api.set_contact_destination(
                body.get("recipient_ref"), body.get("channel"),
                body.get("destination"), body.get("label")))

        # Device token registry: register / activate-deactivate (#65).
        if path == "/api/notifications/device-tokens":
            return self._send_api(api.register_device_token(
                body.get("recipient_ref"), body.get("provider"),
                body.get("token"), body.get("label")))
        dt = re.match(r"^/api/notifications/device-tokens/([^/]+)/active$", path)
        if dt:
            return self._send_api(api.set_device_token_active(
                dt.group(1), bool(body.get("active"))))

        # User accounts: operator creates a login, or activates/deactivates
        # one (#67). No self-service signup — this is the only way an
        # account comes into existence besides the demo seed.
        if path == "/api/accounts":
            return self._send_api(api.create_user_account(
                body.get("username"), body.get("password"), body.get("role"),
                scope=body.get("scope"), actor_id=actor))
        acc = re.match(r"^/api/accounts/([^/]+)/active$", path)
        if acc:
            res = api.set_user_account_active(acc.group(1), bool(body.get("active")))
            if isinstance(res, dict) and "error" not in res and not res.get("active"):
                # Deactivating an account must end any session it already has,
                # not just block future logins.
                SESSIONS.revoke_for_user(api.store, acc.group(1))
            return self._send_api(res)
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
            aid, op = oa.group(1), oa.group(2)
            if op == "unassign":
                return self._send_api(api.unassign_official(aid, actor))
            return self._send_api(api.respond_assignment(aid, op == "accept", actor))

        # /api/games/{gid}/<action>
        m = re.match(r"^/api/games/([^/]+)/(.+)$", path)
        if m:
            gid, action = m.group(1), m.group(2)
            if action == "availability":
                return self._send_api(api.set_availability(
                    gid, pid, body.get("availability_status", "pending"),
                    body.get("response_source", "player"), actor))
            if action == "availability/remind":
                # One-click reminder to players who haven't responded (#89).
                return self._send_api(api.remind_unresponded(
                    gid, body.get("team_id") or scope.get("team_id"), actor))
            if action == "build-roster":
                return self._send_api(api.auto_build_roster(
                    gid, body.get("team_id"), actor))
            if action == "roster/select":
                return self._send_api(api.select_roster(
                    gid, body.get("player_ids", []), actor))
            if action == "roster/remove":
                return self._send_api(api.remove_player(gid, pid, actor))
            if action == "roster/copy-previous":
                return self._send_api(api.copy_previous_roster(
                    gid, body.get("team_id"), actor))
            if action == "officials/assign":
                return self._send_api(api.assign_official(
                    gid, body.get("official_id"), body.get("role", "referee"),
                    actor, override_unavailable=bool(body.get("override_unavailable"))))
            if action == "result":
                return self._send_api(api.record_result(
                    gid, body.get("home_score"), body.get("away_score"), actor))
            if action == "result/approve":
                return self._send_api(api.approve_result(gid, actor))
            if action == "publish":
                return self._send_api(api.publish_game(gid, actor))
            if action == "move":
                return self._send_api(api.move_game(
                    gid, body.get("ice_slot_id"), body.get("reason", ""), actor))
            if action == "substitutes/enroll":
                return self._send_api(api.enroll_substitute(gid, pid, actor))
            if action == "substitutes/withdraw":
                return self._send_api(api.withdraw_substitute(gid, pid, actor))
            sub = re.match(r"^substitutes/([^/]+)/(offer|accept|decline|add-to-roster)$", action)
            if sub:
                player_id, op = sub.group(1), sub.group(2)
                if op == "offer":
                    return self._send_api(api.offer_substitute(
                        gid, player_id, actor, expires_at=body.get("expires_at")))
                fn = {"accept": api.accept_substitute,
                      "decline": api.decline_substitute,
                      "add-to-roster": api.add_substitute_to_roster}[op]
                return self._send_api(fn(gid, player_id, actor))
            coach = {"roster/lock": api.lock_roster,
                     "roster/unlock": api.unlock_roster,
                     "cancel": api.cancel_game}.get(action)
            if coach:
                return self._send_api(coach(gid, actor))

        return self._send_json({"error": {"code": "not_found",
                                          "message": "Unknown endpoint."}}, 404)

    def _handle_setup(self, entity: str, body: dict, actor: str):
        """Dispatch /api/setup/<entity> to the matching facade create method."""
        api = STATE.api
        b = body
        if entity == "league":
            return self._send_api(api.create_league(
                b.get("name"), b.get("country", ""), b.get("timezone", "UTC"), actor))
        if entity == "season":
            return self._send_api(api.create_season(
                b.get("league_id"), b.get("name"),
                b.get("start_date"), b.get("end_date"), actor))
        if entity == "division":
            return self._send_api(api.create_division(
                b.get("season_id"), b.get("name"), b.get("age_group", ""), actor))
        if entity == "club":
            return self._send_api(api.create_club(
                b.get("name"), b.get("country", ""), actor))
        if entity == "team":
            return self._send_api(api.create_team(
                b.get("club_id"), b.get("division_id"), b.get("name"), actor))
        if entity == "venue":
            return self._send_api(api.create_venue(
                b.get("name"), b.get("address", ""), b.get("timezone", "UTC"), actor))
        if entity == "rink":
            return self._send_api(api.create_rink(
                b.get("venue_id"), b.get("name"), actor))
        if entity == "ice-slot":
            return self._send_api(api.create_ice_slot(
                b.get("rink_id"), b.get("start_time"), b.get("end_time"),
                b.get("slot_type", "game"), actor))
        if entity == "game":
            return self._send_api(api.create_game(
                b.get("season_id"), b.get("division_id"), b.get("home_team_id"),
                b.get("away_team_id"), b.get("ice_slot_id"),
                allow_division_override=bool(b.get("allow_division_override")),
                actor_id=actor))
        if entity == "official":
            return self._send_api(api.create_official(
                b.get("name"), b.get("home_club_id"), actor))
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
