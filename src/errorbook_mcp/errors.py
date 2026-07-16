from __future__ import annotations

from typing import Any


class ErrorbookError(Exception):
    code = "ERRORBOOK_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            },
        }


class ValidationError(ErrorbookError):
    code = "VALIDATION_ERROR"


class NotFoundError(ErrorbookError):
    code = "NOT_FOUND"


class ConflictError(ErrorbookError):
    code = "CONFLICT"


class VersionConflictError(ConflictError):
    code = "VERSION_CONFLICT"


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class ExportError(ErrorbookError):
    code = "EXPORT_FAILED"
