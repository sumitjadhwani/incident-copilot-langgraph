"""Domain errors with the HTTP status they map to."""

from __future__ import annotations


class CopilotError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthorized(CopilotError):
    status_code = 401
    code = "unauthorized"


class Forbidden(CopilotError):
    status_code = 403
    code = "forbidden"


class NotFound(CopilotError):
    status_code = 404
    code = "not_found"


class Conflict(CopilotError):
    status_code = 409
    code = "conflict"
