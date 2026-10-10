"""Exceptions for the LLM adapter module.

All adapter-specific errors inherit from LLMAdapterError to allow
callers to catch any adapter error with a single base class.
Credentials and secrets are strictly excluded from exception messages.
"""
from __future__ import annotations


class LLMAdapterError(Exception):
    """Base exception for all LLM adapter operations."""


class LLMConfigurationError(LLMAdapterError):
    """Raised when adapter configuration (e.g. API key) is missing or invalid."""


class LLMConnectionError(LLMAdapterError):
    """Raised when connection to the LLM endpoint fails (network unreachable, DNS, etc.)."""


class LLMTimeoutError(LLMAdapterError):
    """Raised when an LLM request exceeds the configured timeout."""


class LLMAPIError(LLMAdapterError):
    """Raised when the LLM service returns an HTTP error status.

    Attributes:
        status_code: HTTP response status code (e.g. 400, 401, 500).
        message: Sanitized error message (secrets never leaked).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMRateLimitError(LLMAPIError):
    """Raised when the LLM service returns an HTTP 429 Too Many Requests."""


class LLMResponseValidationError(LLMAdapterError):
    """Raised when LLM response is malformed, not valid JSON, or does not conform to the schema."""


class LLMReplayError(LLMAdapterError):
    """Raised when replay mode is enabled but no recorded response exists for the prompt."""
