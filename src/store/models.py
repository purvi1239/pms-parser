"""Data models for database records stored in SQLite."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.fingerprint.models import FormatFingerprint
from src.models.enums import DocumentStatus, StatementType
from src.models.models import Statement
from src.validator.models import ValidationFinding


class DocumentRecord(BaseModel):
    """Metadata and processing state for a single ingested document."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    file_path: str
    file_hash: str
    status: DocumentStatus = DocumentStatus.CLASSIFIED
    statement_type: StatementType | None = None
    format_id: str | None = None
    spec_version_id: str | None = None
    fingerprint: FormatFingerprint | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class StatementRecord(BaseModel):
    """Container associating a parsed Statement domain model with a document."""

    model_config = ConfigDict(validate_assignment=True)

    document_id: str
    statement: Statement
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ValidationRecord(BaseModel):
    """Persisted validation findings and outcome for a parsed statement."""

    model_config = ConfigDict(validate_assignment=True)

    id: int | None = None
    document_id: str
    final_status: DocumentStatus
    passed: bool
    checks_run: list[str] = Field(default_factory=list)
    checks_skipped: list[str] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProcessingAttemptRecord(BaseModel):
    """History entry tracking processing and reparse attempts for a document."""

    model_config = ConfigDict(validate_assignment=True)

    id: int | None = None
    document_id: str
    attempt_number: int
    status: DocumentStatus
    format_id: str | None = None
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
