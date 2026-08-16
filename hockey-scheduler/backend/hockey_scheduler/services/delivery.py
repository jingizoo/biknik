"""Notification delivery queue + mock worker (#58).

When a feed notification is emitted it is also fanned out to one
:class:`NotificationDelivery` row per channel (email, push). A worker later
drains the queue through a *mock* sender — there is no real SMTP / APNs / FCM
in this slice. Failed sends are retried until an attempt budget is exhausted.

Everything here is pure and deterministic: the clock is injected and the
sender is pluggable, so success / failure / retry are all unit-testable.
"""

import threading
from datetime import datetime, timezone

from ..domain import (
    ACCESS_ALLOWED,
    DataAccessLog,
    DeliveryStatus,
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    SensitiveFieldCategory,
)
from ..domain.errors import ValidationError
from . import visibility_policy
from .email_transport import DryRunEmailTransport
from .push_transport import DryRunPushTransport

# Channels every notification fans out to in this slice.
DEFAULT_CHANNELS = (NotificationChannel.EMAIL, NotificationChannel.PUSH)

# How many times a single delivery is attempted before it is left failed.
MAX_ATTEMPTS = 3

# Worker-loop defaults (#79). Disabled by default so tests and the demo never
# spin a background thread unless one is explicitly asked for.
DEFAULT_WORKER_INTERVAL = 30
DEFAULT_WORKER_BATCH = 50


def recipient_ref(notification) -> str:
    """Who a notification's deliveries target, derived from its audience (#59).

    Officials and coaches resolve to the specific official / team the
    notification was addressed to; scheduler and public map to shared
    operator / broadcast targets. This is the routing key a real transport
    would later look up a mailbox or device token by.

    Targeted audiences fail closed (#60): an OFFICIAL / COACH notification with
    no ``audience_ref`` — or an unknown audience — raises rather than silently
    broadening to ``public``, which can now point at a stored contact.
    """
    aud = notification.audience
    ref = notification.audience_ref
    if aud == NotificationAudience.OFFICIAL:
        if not ref:
            raise ValidationError("An official notification needs an audience_ref.")
        return "official:" + ref
    if aud == NotificationAudience.COACH:
        if not ref:
            raise ValidationError("A coach notification needs an audience_ref.")
        return "team:" + ref
    if aud == NotificationAudience.PLAYER:
        if not ref:
            raise ValidationError("A player notification needs an audience_ref.")
        return "player:" + ref
    if aud == NotificationAudience.SCHEDULER:
        return "scheduler"
    if aud == NotificationAudience.PUBLIC:
        return "public"
    raise ValidationError(f"Unsupported notification audience '{aud}'.")


def destination_for(recipient: str, channel) -> str:
    """A placeholder per-channel address for a recipient (#59).

    No real mailbox or device token exists in this slice, so we synthesize an
    obviously-fictional address. The ``.invalid`` TLD is reserved and never
    routes, which keeps the mock worker from ever contacting anything real.
    """
    slug = recipient.replace(":", "-")
    if channel == NotificationChannel.EMAIL:
        return slug + "@notify.invalid"
    return "push-token:" + slug


def resolve_destination(store, recipient: str, channel) -> str:
    """The real stored destination for (recipient, channel), else a placeholder.

    Resolution order:
      1. push only — the recipient's first *active* device token (#65);
      2. an *active* registered contact destination (#60);
      3. the synthesized ``.invalid`` / ``push-token:`` placeholder (#59),
         so a delivery always has somewhere (fictional) to go.

    A retired contact (``active=False``, #232 review 6) is never resolved
    to — retiring one to clear a Player/Official delete's dependency must
    actually stop it from being live, not just stop it from blocking the
    delete. Every retry/re-resolution (below) re-checks this, so a delivery
    enqueued before retirement falls back to the placeholder on its very
    next attempt rather than continuing to reach the retired address.

    Audit-free by design — several call sites (import/reactivation checks,
    contact-registry tests, device-token tests) need the resolved value
    with no system-attributed DataAccessLog side effect. The delivery
    worker's OWN two call sites (below) do not use this function directly;
    they use ``_resolve_and_audit_destination``, which performs the SAME
    resolution but from exactly ONE store read shared with its audit
    decision (#426 round-2 review finding 3 — see that function's own
    docstring for why splitting the read from the audit, as this function
    and a separate audit call used to be combined, is unsafe).
    """
    if channel == NotificationChannel.PUSH:
        token = store.active_device_token_for(recipient)
        if token is not None and token.token:
            return token.token
    stored = store.get_contact_destination(recipient, channel)
    if stored is not None and stored.destination and stored.active:
        return stored.destination
    return destination_for(recipient, channel)


def _resolve_and_audit_destination(store, clock, recipient: str, channel,
                                   request_id: str,
                                   purpose: str = "delivery_resolve") -> str:
    """Resolve (recipient, channel)'s real delivery destination AND, if a
    stored ContactDestination row is what that resolution actually used,
    durably audit reading it — both derived from exactly ONE store read
    (#426 round-2 review finding 3).

    The bug this replaces: ``enqueue()`` and
    ``DeliveryWorker._process_pending_locked()`` used to call a separate
    ``_audit_system_contact_read()`` (its OWN
    ``store.get_contact_destination()`` call) and THEN, independently,
    ``resolve_destination()`` (a SECOND, unrelated
    ``store.get_contact_destination()`` call) — two unlocked reads with no
    shared transaction or snapshot between them. A concurrent upsert or
    retire landing in the gap could make the audit describe a row that was
    never actually delivered to, or — the reviewer's own repro — let the
    delivery use a destination that observed a DIFFERENT (later) write
    than the one the audit recorded (or never audited at all): "the
    delivery used race-secret@example.com while list_data_access() stayed
    empty." Reading ONCE and deriving both outputs from that SAME value
    closes the gap structurally — there is no second read left to race
    against the first, and no way for the returned destination and the
    audited row to describe different snapshots.

    The READ half runs under ``store.transaction(read_only=True)`` —
    NOT a plain (write-capable) ``store.transaction()``. A write-capable
    transaction takes SQLite's file-level RESERVED lock at ``BEGIN``
    (see ``SqlStore.transaction``'s own docstring) for its ENTIRE
    duration, which would make this purely-reading half of the function
    block every OTHER connection's write for as long as it runs — and,
    worse, DEADLOCK against the very concurrent writer this fix exists to
    be race-safe against: that writer's own commit cannot proceed until
    this transaction releases the lock, and this transaction was never
    going to release it before observing whatever the test/caller was
    waiting to see the writer commit. ``read_only=True`` takes SQLite's
    weaker SHARED lock instead (compatible with another connection's
    RESERVED — see that flag's own docstring) — this read still observes
    one consistent value, and it isn't the transaction's lock STRENGTH
    that makes the audit and the destination agree, it is that there is
    only ONE read total, feeding both.

    The AUDIT WRITE (when it fires) happens SEPARATELY, in
    ``add_data_access``'s own transaction, exactly as it did before this
    fix — an audit write that failed for any reason still propagates as
    an exception rather than silently letting the destination be used
    with no trace of it (matching ``_refuse_sensitive_read``'s OWN "every
    access attempt leaves a trace" contract), it is simply not forced
    into the SAME lock-holding unit as the read.

    Resolution order mirrors ``resolve_destination()``'s own (kept as a
    separate, audit-free, single-shot utility several OTHER call sites —
    none of them the delivery worker — still use directly, see its
    docstring):
      1. push only — the recipient's first *active* device token (#65),
         never audited (not a ContactDestination read at all);
      2. an *active* registered contact destination (#60) — audited HERE,
         from the SAME row this branch returns, when reached;
      3. the synthesized placeholder (#59) — never audited (nothing
         stored was disclosed).

    Attributed to ``visibility_policy.SYSTEM_PRINCIPAL`` (never a
    caller/session principal — there is none here: this runs from the
    background worker loop and the manual drain endpoint alike, neither of
    which acts on behalf of a signed-in user), with the given ``purpose``
    and a ``request_id`` shared by every row ONE enqueue/drain call writes
    (#426 review finding 3's original "one request shares one safe id",
    generalised to one worker run).
    """
    with store.transaction(read_only=True):
        if channel == NotificationChannel.PUSH:
            token = store.active_device_token_for(recipient)
            if token is not None and token.token:
                return token.token
        stored = store.get_contact_destination(recipient, channel)
    if stored is None or not stored.destination or not stored.active:
        return destination_for(recipient, channel)
    # `id` is deliberately left unset: the store assigns it (#426
    # round-2 review finding 1) — see domain/privacy.py's DURABLE ID
    # ALLOCATION section.
    store.add_data_access(DataAccessLog(
        category=SensitiveFieldCategory.CONTACT_DESTINATION,
        subject_type="recipient",
        subject_id=visibility_policy.canonical_subject_id(recipient),
        purpose=purpose,
        at=clock(),
        actor_user_id=None,
        actor_role="system",
        outcome=ACCESS_ALLOWED,
        request_id=request_id))
    return stored.destination


def channel_enabled(store, recipient: str, channel) -> bool:
    """Whether ``recipient`` accepts deliveries on ``channel`` (#81).

    A stored preference with ``enabled=False`` opts the channel out; absent
    preferences mean the channel is on (existing default behavior). The in-app
    feed is always delivered and is not gated here.
    """
    pref = store.get_notification_preference(recipient, channel)
    return True if pref is None else pref.enabled


def _guardian_recipient_refs(store, player_id) -> list:
    """Verified guardians linked to ``player_id``, as delivery recipient refs
    (#32). An unverified link carries no authority — same gate the guardian
    action routes use (#26) — so it gets no delivery either."""
    return ["guardian:" + g.guardian_user_id
            for g in store.guardian_links_for_player(player_id) if g.verified]


def enqueue(store, notification, channels=DEFAULT_CHANNELS, clock=None):
    """Create the pending delivery rows for a freshly emitted notification.

    A channel the recipient has disabled in their preferences (#81) is skipped
    — no delivery row is created for it — so the resolver honors opt-outs.

    A PLAYER-addressed notification also reaches any verified guardian linked
    to that junior (#32) — a junior may not check their own device, so their
    guardian needs the same push/email delivery. This fans out here, in the
    delivery layer itself, so every existing and future PLAYER-audience
    emission site (today: substitute offers) gets it automatically without
    each call site needing to know guardians exist.

    ``clock`` defaults to the real UTC clock (#426 review finding 2's
    SYSTEM-attributed audit needs a timestamp; existing call sites — none of
    which had a clock to pass — keep working unchanged). Every destination
    this call resolves from a REAL stored ContactDestination shares ONE
    request_id, minted once per call.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    request_id = visibility_policy.mint_request_id()
    recipients = [recipient_ref(notification)]
    if notification.audience == NotificationAudience.PLAYER:
        recipients.extend(
            _guardian_recipient_refs(store, notification.audience_ref))
    created = []
    for recipient in recipients:
        for channel in channels:
            if not channel_enabled(store, recipient, channel):
                continue
            # ONE read drives both the destination and its audit (#426
            # round-2 review finding 3) — see
            # _resolve_and_audit_destination's own docstring.
            destination = _resolve_and_audit_destination(
                store, clock, recipient, channel, request_id)
            d = NotificationDelivery(
                id=store.next_id("notif_delivery"),
                notification_id=notification.id,
                channel=channel,
                recipient_ref=recipient,
                destination=destination,
            )
            created.append(store.add_notification_delivery(d))
    return created


def mock_sender(delivery, notification) -> None:
    """A no-op sender for non-email channels (push is still mocked, #62).

    Raising an exception signals a failed send, which the worker records and
    retries.
    """
    return None


def make_delivery_sender(email_transport, push_transport=None):
    """A sender that routes each delivery to its channel transport (#62/#64).

    Email deliveries go through ``email_transport`` and push deliveries through
    ``push_transport`` (a safe dry-run by default). Any exception from either
    path signals a failed send.
    """
    push_transport = push_transport or DryRunPushTransport()

    def sender(delivery, notification) -> None:
        if delivery.channel == NotificationChannel.EMAIL:
            email_transport.send(delivery, notification)
        else:
            push_transport.send(delivery, notification)
    return sender


class DeliveryWorker:
    """Drains pending notification deliveries through the configured transports.

    Email and push deliveries go through their transports (both safe dry-runs
    by default, so nothing is sent for real unless a real transport is
    configured). A fully custom ``sender`` can still be injected for tests.
    """

    def __init__(self, store, clock, sender=None,
                 max_attempts: int = MAX_ATTEMPTS, email_transport=None,
                 push_transport=None):
        self.store = store
        self.clock = clock
        self.email_transport = email_transport or DryRunEmailTransport()
        self.push_transport = push_transport or DryRunPushTransport()
        self.sender = sender or make_delivery_sender(
            self.email_transport, self.push_transport)
        self.max_attempts = max_attempts
        # One process-wide lock guards the read-send-save critical section so
        # the manual drain endpoint and the background worker loop (#79) cannot
        # both claim the same pending rows and send them twice. Both paths call
        # process_pending on the SAME worker instance, so the lock must live on
        # the worker, not on the loop.
        self._process_lock = threading.Lock()

    def process_pending(self, limit=None) -> dict:
        """Attempt every deliverable row once; return a run summary.

        Deliverable = still ``pending``, or ``failed`` with attempts left.
        A row that fails its final attempt is moved to ``dead_lettered`` and is
        not picked up again until an operator retries it (#80). ``limit`` caps
        how many rows are processed in one call (the worker loop's batch size,
        #79); ``None`` processes all of them.

        Serialized by ``self._process_lock`` so concurrent callers (manual
        drain + background loop) drain the queue one batch at a time rather
        than double-sending overlapping rows.
        """
        with self._process_lock:
            return self._process_pending_locked(limit)

    def _process_pending_locked(self, limit=None) -> dict:
        processed = sent = failed = dead_lettered = 0
        rows = self.store.pending_deliveries(self.max_attempts)
        if limit is not None:
            rows = rows[:limit]
        # One correlation id for this WHOLE drain run (#426 review finding
        # 2/3) — every row's re-resolution below that actually reads a
        # stored ContactDestination shares it, mirroring the facade's own
        # "one request shares one safe id" contract.
        run_request_id = visibility_policy.mint_request_id()
        for d in rows:
            notification = self.store.get_notification_feed(d.notification_id)
            # Re-resolve the destination on every attempt so a contact or
            # device token registered AFTER emission applies to queued and
            # retrying deliveries (they would otherwise fail forever on the
            # placeholder stamped at enqueue time).
            if d.recipient_ref:
                # ONE read drives both the destination and its audit
                # (#426 round-2 review finding 3) — see
                # _resolve_and_audit_destination's own docstring.
                d.destination = _resolve_and_audit_destination(
                    self.store, self.clock, d.recipient_ref, d.channel,
                    run_request_id)
            now = self.clock()
            d.attempts += 1
            d.last_attempt_at = now
            try:
                self.sender(d, notification)
            except Exception as exc:  # a mock/real transport failure
                d.last_error = str(exc) or exc.__class__.__name__
                if d.attempts >= self.max_attempts:
                    # Attempt budget exhausted: park it for the operator (#80).
                    d.status = DeliveryStatus.DEAD_LETTERED
                    d.dead_lettered_at = now
                    d.next_attempt_at = None
                    dead_lettered += 1
                else:
                    d.status = DeliveryStatus.FAILED
                    d.next_attempt_at = now  # eligible immediately in this slice
                failed += 1
            else:
                d.status = DeliveryStatus.SENT
                d.sent_at = now
                d.last_error = None
                d.next_attempt_at = None
                sent += 1
            self.store.save_notification_delivery(d)
            processed += 1
        return {"processed": processed, "sent": sent, "failed": failed,
                "dead_lettered": dead_lettered}


class DeliveryLoop:
    """A controllable loop around :class:`DeliveryWorker` (#79).

    Turns manual drains into an opt-in background worker: ``run_once`` for a
    single bounded batch, ``start``/``stop`` for a daemon loop on a fixed
    interval. Disabled by default so nothing runs unless explicitly enabled —
    tests and the demo never spin a thread implicitly. The underlying worker
    (and its dry-run/live transports) is unchanged; the loop only decides
    *when* and *how many* rows to process.
    """

    def __init__(self, worker: DeliveryWorker, enabled: bool = False,
                 interval_seconds: int = DEFAULT_WORKER_INTERVAL,
                 batch_size: int = DEFAULT_WORKER_BATCH):
        self.worker = worker
        self.enabled = bool(enabled)
        # Preserve a fractional interval (tests drive sub-second loops); only
        # clamp non-positive values up to a small positive floor so a bad
        # config can never spin a hot loop.
        self.interval_seconds = max(float(interval_seconds), 0.001)
        self.batch_size = int(batch_size)
        self._thread = None
        self._stop = threading.Event()

    def run_once(self) -> dict:
        """Process a single batch (up to ``batch_size`` rows) and return the
        run summary. Works regardless of ``enabled`` — it is the manual drain."""
        return self.worker.process_pending(limit=self.batch_size)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the background loop. No-op (returns False) when disabled or
        already running; returns True when a thread was started."""
        if not self.enabled or self.is_running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="delivery-worker")
        self._thread.start()
        return True

    def _loop(self) -> None:
        # Wait first so start() returns promptly; keep looping until stopped.
        # A transport error in one batch must not kill the loop.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        """Serializable worker status for the delivery overview (#79)."""
        return {"enabled": self.enabled, "running": self.is_running(),
                "interval_seconds": self.interval_seconds,
                "batch_size": self.batch_size}


def delivery_loop_from_env(worker: DeliveryWorker, env) -> DeliveryLoop:
    """Build a DeliveryLoop from environment config (#79).

    DELIVERY_WORKER_ENABLED (truthy: 1/true/yes/on) enables the loop;
    DELIVERY_WORKER_INTERVAL and DELIVERY_WORKER_BATCH override the interval
    (seconds) and batch size. Absent/blank → safe disabled defaults.
    """
    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    def _int(v, default):
        try:
            n = int(str(v).strip())
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    return DeliveryLoop(
        worker,
        enabled=_truthy(env.get("DELIVERY_WORKER_ENABLED")),
        interval_seconds=_int(env.get("DELIVERY_WORKER_INTERVAL"),
                              DEFAULT_WORKER_INTERVAL),
        batch_size=_int(env.get("DELIVERY_WORKER_BATCH"), DEFAULT_WORKER_BATCH))
