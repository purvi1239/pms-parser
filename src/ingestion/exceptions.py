"""Exceptions for directory and file ingestion."""
from __future__ import annotations


class IngestionError(Exception):
    """Base exception for all ingestion-related errors."""


class InvalidDirectoryError(IngestionError, ValueError):
    """Base exception for invalid ingestion directory paths."""


class DirectoryNotFoundError(InvalidDirectoryError, FileNotFoundError):
    """Raised when the specified directory path does not exist on disk."""


class NotADirectoryPathError(InvalidDirectoryError, NotADirectoryError):
    """Raised when the specified path exists but is a file or non-directory."""
