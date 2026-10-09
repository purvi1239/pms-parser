"""Exceptions raised by IR and PDF extraction operations."""
from __future__ import annotations


class PDFExtractionError(Exception):
    """Raised when a PDF file cannot be opened, decrypted, or extracted."""
    pass
