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
