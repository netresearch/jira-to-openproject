#!/usr/bin/env python3
"""Common exceptions for all client modules."""


class ClientError(Exception):
    """Base exception for all client errors."""


class ConnectionError(ClientError):
    """Error when connection to a service fails."""


class QueryExecutionError(ClientError):
    """Error when executing a query."""


class RecordNotFoundError(ClientError):
    """Error when a record is not found."""


class JsonParseError(ClientError):
    """Error when parsing JSON output."""


class AuthenticationError(ClientError):
    """Error when authentication fails."""


class ResourceNotFoundError(ClientError):
    """Error when a resource is not found."""


class CaptchaError(ClientError):
    """Error when CAPTCHA challenge is detected."""


class ApiError(ClientError):
    """General API error."""


class RateLimitError(ClientError):
    """Error when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
