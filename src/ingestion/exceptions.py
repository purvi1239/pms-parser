"""Exceptions for directory, file, and email ingestion."""
from __future__ import annotations


class IngestionError(Exception):
    """Base exception for all ingestion-related errors."""


class InvalidDirectoryError(IngestionError, ValueError):
    """Base exception for invalid ingestion directory paths."""


class DirectoryNotFoundError(InvalidDirectoryError, FileNotFoundError):
    """Raised when the specified directory path does not exist on disk."""


class NotADirectoryPathError(InvalidDirectoryError, NotADirectoryError):
    """Raised when the specified path exists but is a file or non-directory."""


class EmailIngestionError(IngestionError):
    """Base exception for email attachment ingestion errors."""


class MalformedEmailError(EmailIngestionError, ValueError):
    """Raised when an email message payload is malformed or unparseable."""
