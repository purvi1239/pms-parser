"""Validation finding and result models for parsed statement validation.

These models are intentionally separate from ParseResult to preserve the
parser's extracted values unchanged — the validator only annotates, never mutates.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DocumentStatus


class ValidationSeverity(str, Enum):
    """Severity level of a single validation finding."""

    ERROR = "error"
    # Definite data issue whose magnitude exceeds configured tolerance.
    # Elevates final_status to NEEDS_REVIEW.

    WARNING = "warning"
    # Potential issue, incomplete check, or inapplicable check due to missing data.
    # Elevates final_status to PARSED_WITH_WARNINGS when no errors are present.


class ValidationFinding(BaseModel):
    """A single structured finding produced by one validation check."""

    model_config = ConfigDict(validate_assignment=True)

    check: str
    """Stable identifier of the check that raised this finding, e.g. 'cash_reconciliation'."""

    severity: ValidationSeverity

    message: str
    """Human-readable explanation of the finding."""

    field: str | None = None
    """Field name this finding relates to (e.g. 'summary.closing_cash')."""

    actual: str | None = None
    """Actual value as extracted or computed (stringified for display)."""

    expected: str | None = None
    """Expected or allowable value (stringified for display)."""


class ValidationResult(BaseModel):
    """Aggregated output of all validator checks run against a ParseResult."""

    model_config = ConfigDict(validate_assignment=True)

    findings: list[ValidationFinding] = Field(default_factory=list)

    checks_run: list[str] = Field(default_factory=list)
    """Names of checks that were attempted (had enough data to run)."""

    checks_skipped: list[str] = Field(default_factory=list)
    """Names of checks that were skipped due to missing required data."""

    final_status: DocumentStatus = DocumentStatus.PARSED
    """Recommended DocumentStatus after validation.

    Computed automatically based on findings:
    - ERROR finding  → NEEDS_REVIEW
    - WARNING finding → PARSED_WITH_WARNINGS
    - No findings    → PARSED (inherited from input or set by caller)
    """

    # ------------------------------------------------------------------ #
    # Convenience accessors                                                #
    # ------------------------------------------------------------------ #

    @property
    def errors(self) -> list[ValidationFinding]:
        """Return only error-severity findings."""
        return [f for f in self.findings if f.severity == ValidationSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationFinding]:
        """Return only warning-severity findings."""
        return [f for f in self.findings if f.severity == ValidationSeverity.WARNING]

    @property
    def passed(self) -> bool:
        """True when no ERROR-level findings were produced."""
        return not self.errors
