"""Domain errors.

Each error carries a stable ``code`` matching the API contract so the API
layer can translate it into the structured ``{"error": {...}}`` shape without
leaking Python exceptions across the boundary.
"""


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


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
