"""Exceptions raised by parsing specification models and SpecStore operations."""
from __future__ import annotations


class SpecError(Exception):
    """Base exception for specification and SpecStore errors."""
    pass


class SpecNotFoundError(SpecError):
    """Raised when a requested parsing specification cannot be found."""
    pass


class SpecValidationError(SpecError):
    """Raised when a parsing specification is invalid, corrupted, or violates schema."""
    pass


class DuplicateSpecError(SpecError):
    """Raised when attempting to save a duplicate specification without overwrite=True."""
    pass
