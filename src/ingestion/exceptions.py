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


class IMAPError(EmailIngestionError):
    """Base exception for IMAP communication and mailbox errors."""


class IMAPAuthError(IMAPError):
    """Raised when IMAP authentication fails or credentials are missing."""


class IMAPConnectionError(IMAPError):
    """Raised when connecting or establishing SSL to the IMAP server fails."""


class IMAPFetchError(IMAPError):
    """Raised when selecting a mailbox or searching/fetching messages fails."""
