"""Exceptions for the persistent SQLite statement store."""
from __future__ import annotations


class StoreError(Exception):
    """Base exception for all persistence and storage operations."""


class DocumentNotFoundError(StoreError):
    """Raised when a requested document record does not exist in the store."""


class DuplicateDocumentError(StoreError):
    """Raised when attempting to insert a document with an existing unique identifier or file hash."""


class StatementNotFoundError(StoreError):
    """Raised when parsed statement data is requested for a document that has none."""


class DatabaseSchemaError(StoreError):
    """Raised when database schema initialization or migration fails."""
