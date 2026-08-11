"""Domain errors.

Each error carries a stable ``code`` matching the API contract so the API
layer can translate it into the structured ``{"error": {...}}`` shape without
leaking Python exceptions across the boundary.
"""


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        # Optional machine-readable context (e.g. a conflict ``reason`` and the
        # ids involved) so a client can explain *why* an action failed without
        # parsing the English message. See the conflict side panel (#43).
        self.details = details or {}

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"error": err}


class NotFoundError(DomainError):
    code = "not_found"


class ValidationError(DomainError):
    code = "validation_error"


class RosterLockedError(DomainError):
    code = "roster_locked"


class GameCancelledError(DomainError):
    code = "game_cancelled"


class AlreadySelectedError(DomainError):
    code = "already_selected"


class NotEnrolledError(DomainError):
    code = "not_enrolled"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class SlotAlreadyFilledError(DomainError):
    code = "slot_already_filled"


class NotEligibleError(DomainError):
    code = "not_eligible"


class ScheduleConflictError(DomainError):
    code = "schedule_conflict"


class DivisionMismatchError(DomainError):
    code = "division_mismatch"


class NotAuthorizedError(DomainError):
    code = "forbidden"


class ActiveContextRequiredError(DomainError):
    """The caller has no EXPLICIT, still-valid persisted Program/Season tuple
    and asked to MUTATE (#393 PR A, #409).

    Deliberately NOT an authorization error. These callers may legitimately
    switch context and perform the very same write; what is missing is a
    CHOICE, not a privilege. `ContextService._fallback` auto-selects a Program
    for read-only landing and navigation, and that convenience must never be
    laundered into the operator's decision at a write boundary — a stale saved
    selection would otherwise silently retarget their work into a Program they
    navigated away from.

    ONE code for the whole rule, defined here exactly once, so the ice
    availability commit (#393 PR A), the scheduler draft batches and the
    guarded setup mutations cannot drift into three wire contracts for one
    condition. `web.server.ERROR_HTTP_STATUS` maps it to 409: recoverable, and
    distinct from the generic 404 a target the caller may not touch returns.
    """

    code = "active_context_required"


class HasDependenciesError(DomainError):
    """A record can't be deleted because dependent records/history exist.

    Carries a structured ``details`` payload so the client can explain exactly
    what blocks the deletion (a list of ``{type, count, names}`` groups) rather
    than only rendering an English sentence. Deletion is never a silent cascade
    — the caller must clear the dependents first (#215 safe destructive actions).
    """

    code = "has_dependencies"


class AlreadyClaimedError(DomainError):
    """A one-time installation claim has already been consumed (#174)."""

    code = "already_claimed"


class InvalidSetupCodeError(DomainError):
    """The submitted one-time setup code did not match (#174)."""

    code = "invalid_setup_code"


class ClaimUnavailableError(DomainError):
    """The deployment is not in a posture where browser claim is allowed."""

    code = "claim_unavailable"


class IntegrityConflictError(DomainError):
    """A write violated a database integrity rule (unique / FK / check / not-null).

    Raised by the store's exception-translation boundary (#201 Slice 2) after a
    failed transaction has rolled back. The message is generic and secret-free;
    ``details["reason"]`` carries a stable machine-readable subtype (e.g.
    ``"unique_violation"``) with no table, column, constraint name, or driver
    text. A specific workflow may catch this to raise a more precise domain
    error (e.g. a duplicate installation-claim → AlreadyClaimedError).
    """

    code = "conflict"


class ConcurrencyConflictError(DomainError):
    """A write failed due to concurrent activity (serialization / deadlock / lock).

    Translated from the driver by the store boundary (#201 Slice 2) after
    rollback. ``details`` carries a stable ``reason`` and ``retryable: True`` so
    a caller can safely retry; the message never leaks driver or connection
    details.
    """

    code = "concurrency_conflict"
