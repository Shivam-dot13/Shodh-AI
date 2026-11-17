"""Three distinct failure families, so a client can tell them apart.

* ValidationError      -> 422, request was malformed (shape, types, ranges).
* BusinessRuleError    -> 4xx, request was well formed but the contest rules reject it.
* InfrastructureError  -> 503, our side failed; the caller may retry.

Every error body is {"error": {"type", "code", "message", "details", "request_id"}}.
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    error_type = "business_rule"
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details

    def body(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "type": self.error_type,
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class BusinessRuleError(AppError):
    status_code = 409
    error_type = "business_rule"
    code = "business_rule_violation"


class ContestClosed(BusinessRuleError):
    code = "contest_closed"


class ContestNotStarted(BusinessRuleError):
    code = "contest_not_started"


class NotAParticipant(BusinessRuleError):
    code = "not_a_participant"


class AttemptLimitReached(BusinessRuleError):
    code = "attempt_limit_reached"


class IdempotencyConflict(BusinessRuleError):
    code = "idempotency_key_conflict"


class StaleJudgeResult(BusinessRuleError):
    code = "stale_judge_result"


class IllegalStateTransition(BusinessRuleError):
    code = "illegal_state_transition"


class ValidationError(AppError):
    status_code = 422
    error_type = "validation"
    code = "invalid_request"


class AuthenticationError(AppError):
    status_code = 401
    error_type = "authentication"
    code = "unauthenticated"


class AuthorizationError(AppError):
    status_code = 403
    error_type = "authorization"
    code = "forbidden"


class NotFoundError(AppError):
    """Cross-tenant reads land here too: knowing an id must not confirm existence."""

    status_code = 404
    error_type = "not_found"
    code = "not_found"


class InfrastructureError(AppError):
    status_code = 503
    error_type = "infrastructure"
    code = "infrastructure_failure"
