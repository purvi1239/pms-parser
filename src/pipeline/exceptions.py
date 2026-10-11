"""Exceptions raised during pipeline processing and orchestration."""
from __future__ import annotations


class PipelineError(Exception):
    """Base exception for all pipeline orchestration failures."""


class DocumentExtractionError(PipelineError):
    """Raised when extracting intermediate representation from a document fails."""


class PasswordRequiredError(PipelineError):
    """Raised when an encrypted document requires a password that was not provided or failed."""


class UnsupportedFormatPipelineError(PipelineError):
    """Raised when a document format is completely unknown and cannot be parsed."""


class DriftDetectedPipelineError(PipelineError):
    """Raised when a template drift is detected and document is quarantined."""


class PipelineStorageError(PipelineError):
    """Raised when persisting pipeline state, statements, or validations fails."""
