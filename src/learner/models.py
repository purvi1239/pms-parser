"""Domain models and data structures for the Learner module.

Defines proposal statuses, human feedback containers, configuration,
attempt tracking, and learning outcomes.
"""
from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DocumentStatus
from src.models.models import ParseResult
from src.specs.models import ParsingSpec
from src.validator.models import ValidationResult


class ProposalStatus(str, Enum):
    """Lifecycle and evaluation status of a proposed parsing specification."""

    ACCEPTED = "accepted"
    """Candidate specification passed all engine extraction and validator checks and was activated."""

    PROPOSED = "proposed"
    """Candidate specification passed validation checks and is retained separately as a draft proposal."""

    REJECTED = "rejected"
    """Candidate specification failed validation or failed to match human corrections after exhausting repair attempts."""

    INVALID_FEEDBACK = "invalid_feedback"
    """Human review feedback provided was empty, malformed, or internally inconsistent."""


class HumanFeedback(BaseModel):
    """Structured human corrections or review feedback for statement parsing."""

    model_config = ConfigDict(validate_assignment=True)

    format_id: str
    """Target format identifier to update or propose."""

    corrected_metadata: dict[str, Any] = Field(default_factory=dict)
    """Field-level corrections for Statement metadata (e.g. provider_name, client_identifier)."""

    corrected_summary: dict[str, Any] = Field(default_factory=dict)
    """Field-level corrections for Summary totals (e.g. closing_cash, closing_value)."""

    corrected_holdings: list[dict[str, Any]] = Field(default_factory=list)
    """Corrected portfolio holdings rows."""

    corrected_transactions: list[dict[str, Any]] = Field(default_factory=list)
    """Corrected transactions rows."""

    notes: str | None = None
    """Reviewer comments or hints (e.g. 'Holdings header changed to Portfolio Breakdown')."""

    def has_corrections(self) -> bool:
        """Return True if feedback contains at least one actionable correction."""
        return bool(
            self.corrected_metadata
            or self.corrected_summary
            or self.corrected_holdings
            or self.corrected_transactions
        )


class LearnerConfig(BaseModel):
    """Configuration options for the Learner loop."""

    model_config = ConfigDict(validate_assignment=True)

    max_repairs: int = 2
    """Maximum number of repair retries after an initial failed attempt (default 2, total 3 calls)."""

    auto_activate: bool = False
    """If True, successfully validated specs are saved as active specs in SpecStore."""

    temperature: float = 0.0
    """Deterministic LLM sampling temperature."""

    max_tokens: int = 2048
    """Maximum completion tokens for the LLM request to avoid timeouts."""

    model: str | None = None
    """LLM model override (defaults to adapter config)."""

    draft_prefix: str = "draft_"
    """Prefix used when saving non-activated draft/proposal specifications."""


class LearningAttempt(BaseModel):
    """Record of a single attempt in the learning or repair loop."""

    model_config = ConfigDict(validate_assignment=True)

    attempt_number: int
    raw_response: dict[str, Any] | str | None = None
    spec: ParsingSpec | None = None
    parse_result: ParseResult | None = None
    validation_result: ValidationResult | None = None
    errors: list[str] = Field(default_factory=list)
    success: bool = False


class LearningResult(BaseModel):
    """Aggregated outcome of learning or updating a specification."""

    model_config = ConfigDict(validate_assignment=True)

    format_id: str
    status: ProposalStatus
    document_status: DocumentStatus
    spec: ParsingSpec | None = None
    is_active: bool = False
    attempts_count: int = 1
    parse_result: ParseResult | None = None
    validation_result: ValidationResult | None = None
    error_message: str | None = None
    repair_history: list[str] = Field(default_factory=list)
    attempts: list[LearningAttempt] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True if spec generation succeeded and passed all validation checks."""
        return self.status in (ProposalStatus.ACCEPTED, ProposalStatus.PROPOSED)

    @property
    def passed_validation(self) -> bool:
        """Return True if a validation result exists and has passed with zero errors."""
        return self.validation_result is not None and self.validation_result.passed
