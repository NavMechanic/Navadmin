"""Domain errors. Services raise these; main.py maps them to HTTP responses."""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(DomainError):
    status_code, code = 404, "not_found"


class Forbidden(DomainError):
    status_code, code = 403, "forbidden"


class Conflict(DomainError):
    status_code, code = 409, "conflict"


class GateBlocked(DomainError):
    status_code, code = 409, "gate_blocked"


class ValidationFailed(DomainError):
    status_code, code = 422, "validation_failed"


class BudgetExceeded(DomainError):
    status_code, code = 402, "budget_exceeded"


class ProviderError(DomainError):
    status_code, code = 502, "llm_provider_error"
