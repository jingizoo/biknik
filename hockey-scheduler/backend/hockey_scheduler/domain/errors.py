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
