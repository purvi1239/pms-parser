"""Data models and configuration for the pipeline module."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.classifier.models import ClassificationResult
from src.fingerprint.models import FingerprintMatchResult, FormatFingerprint
from src.learner.models import LearningResult
from src.models.enums import DocumentStatus, StatementType
from src.models.models import ParseResult, Statement
from src.validator.config import ValidatorConfig
from src.validator.models import ValidationResult


class PipelineConfig(BaseModel):
    """Configuration options for pipeline execution and orchestration."""

    model_config = ConfigDict(validate_assignment=True)

    enable_learning: bool = False
    """Whether to invoke the Learner when an unknown document format is encountered."""

    auto_activate_learned_spec: bool = False
    """Whether to automatically activate a newly learned spec into SpecStore."""

    store_results: bool = True
    """Whether to persist processing states, statements, and validation findings to StatementStore."""

    passwords: list[str] = Field(default_factory=list)
    """Candidate passwords to attempt for encrypted PDF documents."""

    export_json: bool = False
    """Whether to export parsed Statement JSON files to disk."""

    export_dir: Path | str | None = None
    """Target directory for exported statement JSON files (if export_json is True)."""

    match_threshold: float = 0.90
    """Fingerprint similarity threshold for KNOWN format match."""

    near_match_threshold: float = 0.65
    """Fingerprint similarity threshold for DRIFT_SUSPECTED quarantine."""

    validator_config: ValidatorConfig | None = None
    """Optional configuration overrides for financial validation tolerances."""


class PipelineResult(BaseModel):
    """Complete structured output of end-to-end statement pipeline processing."""

    model_config = ConfigDict(validate_assignment=True)

    document_id: str
    """Unique document identifier assigned in the persistent store."""

    status: DocumentStatus
    """Final lifecycle state of the document (e.g. PARSED, NEEDS_REVIEW, DRIFT_SUSPECTED)."""

    file_path: str
    """Original file path or synthetic source label."""

    file_hash: str
    """SHA-256 digest of the input file or document content."""

    statement_type: StatementType | None = None
    """Identified statement category (PMS or AIF)."""

    format_id: str | None = None
    """Resolved format identifier if recognized or learned."""

    spec_version_id: str | None = None
    """Specification version identifier used during parsing."""

    classification: ClassificationResult | None = None
    """Deterministic classifier output (decision, score, reasons, signals)."""

    fingerprint: FormatFingerprint | None = None
    """Extracted structural layout fingerprint."""

    fingerprint_match: FingerprintMatchResult | None = None
    """Outcome of comparing fingerprint against known templates."""

    parse_result: ParseResult | None = None
    """Parsed statement domain model and extracted tables/summaries."""

    validation_result: ValidationResult | None = None
    """Financial reconciliation and structural validation outcome."""

    learning_result: LearningResult | None = None
    """Outcome of spec learning loop (if invoked for unknown formats)."""

    warnings: list[str] = Field(default_factory=list)
    """Aggregated non-fatal warnings encountered throughout the pipeline."""

    errors: list[str] = Field(default_factory=list)
    """Aggregated fatal or validation errors recorded."""

    is_duplicate: bool = False
    """True if this file was identified as a previously ingested duplicate."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Additional runtime metadata and execution diagnostics."""

    @property
    def success(self) -> bool:
        """Return True if the document was successfully parsed without blocking errors."""
        return self.status in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS)

    @property
    def statement(self) -> Statement | None:
        """Return the extracted Statement model if parsing succeeded."""
        if self.parse_result and self.parse_result.statement:
            return self.parse_result.statement
        return None

    @property
    def is_quarantined(self) -> bool:
        """Return True if document is held in a review or quarantine queue."""
        return self.status in (
            DocumentStatus.DRIFT_SUSPECTED,
            DocumentStatus.NEEDS_REVIEW,
            DocumentStatus.MAYBE,
            DocumentStatus.LEARNING_FAILED,
        )


class BatchPipelineResult(BaseModel):
    """Aggregated summary of processing multiple documents in batch mode."""

    model_config = ConfigDict(validate_assignment=True)

    total_documents: int = 0
    successful: int = 0
    failed: int = 0
    duplicates: int = 0
    quarantined: int = 0
    skipped: int = 0
    results: list[PipelineResult] = Field(default_factory=list)
