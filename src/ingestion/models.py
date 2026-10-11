"""Data models and configuration for folder and email ingestion modules."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.pipeline.models import PipelineResult


class FolderIngestionConfig(BaseModel):
    """Configuration options for folder discovery and batch ingestion."""

    model_config = ConfigDict(validate_assignment=True)

    recursive: bool = False
    """Whether to search subdirectories recursively. Defaults to False (non-recursive)."""

    file_extensions: list[str] = Field(default_factory=lambda: [".pdf"])
    """List of supported file extensions to discover (case-insensitive). Defaults to ['.pdf']."""

    passwords: list[str] = Field(default_factory=list)
    """Optional candidate passwords to pass to the pipeline for encrypted files."""


class FolderIngestionResult(BaseModel):
    """Structured summary and results of a folder ingestion run."""

    model_config = ConfigDict(validate_assignment=True)

    directory: str
    """Resolved directory path that was ingested."""

    total_discovered: int = 0
    """Total number of supported files discovered in the directory."""

    processed: int = 0
    """Total number of files dispatched through the pipeline."""

    successful: int = 0
    """Total number of documents successfully parsed (PARSED or PARSED_WITH_WARNINGS)."""

    failed: int = 0
    """Total number of documents that failed processing or encryption without password."""

    duplicates: int = 0
    """Total number of documents identified as duplicates."""

    quarantined: int = 0
    """Total number of documents quarantined for manual review or learning."""

    skipped: int = 0
    """Total number of files skipped as non-statements."""

    results: list[PipelineResult] = Field(default_factory=list)
    """Ordered per-file PipelineResult objects preserved from pipeline execution."""

    errors: list[str] = Field(default_factory=list)
    """Directory-level errors or operational diagnostic messages."""

    # Convenience alias properties
    @property
    def discovered_files(self) -> int:
        """Alias for total_discovered."""
        return self.total_discovered

    @property
    def total_processed(self) -> int:
        """Alias for processed."""
        return self.processed

    @property
    def success_count(self) -> int:
        """Alias for successful."""
        return self.successful

    @property
    def failure_count(self) -> int:
        """Alias for failed."""
        return self.failed

    @property
    def skipped_count(self) -> int:
        """Alias for skipped."""
        return self.skipped


# Alias for callers who prefer the name FolderIngestionSummary
FolderIngestionSummary = FolderIngestionResult


# ---------------------------------------------------------------------------
# Email Ingestion Models
# ---------------------------------------------------------------------------

class EmailAttachment(BaseModel):
    """Represents an email attachment extracted from an email message."""

    model_config = ConfigDict(validate_assignment=True)

    filename: str | None = None
    """Original attachment filename (e.g. 'statement.pdf')."""

    content: bytes = b""
    """Raw binary content of the attachment."""

    content_type: str | None = None
    """MIME content type if available (e.g. 'application/pdf')."""

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_content(cls, v: Any) -> bytes:
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, bytes):
            return v
        if v is None:
            return b""
        return bytes(v)


class EmailMessageInput(BaseModel):
    """Container for email message metadata and its attachments."""

    model_config = ConfigDict(validate_assignment=True)

    email_id: str | None = None
    """Unique email identifier, message-ID header, or UID."""

    sender: str | None = None
    """Sender email address."""

    subject: str | None = None
    """Email subject line."""

    attachments: list[EmailAttachment] = Field(default_factory=list)
    """List of attachments extracted or supplied with the message."""


# Alias for convenience
EmailMessage = EmailMessageInput


class EmailIngestionConfig(BaseModel):
    """Configuration options for email attachment ingestion."""

    model_config = ConfigDict(validate_assignment=True)

    supported_extensions: list[str] = Field(default_factory=lambda: [".pdf"])
    """Supported attachment extensions (case-insensitive). Defaults to ['.pdf']."""

    passwords: list[str] = Field(default_factory=list)
    """Optional candidate passwords to pass to the pipeline for encrypted files."""

    deduplicate_batch: bool = True
    """Whether to deduplicate identical attachment content hashes within a batch."""


class EmailIngestionResult(BaseModel):
    """Structured summary and results of email attachment ingestion."""

    model_config = ConfigDict(validate_assignment=True)

    total_emails: int = 0
    """Total number of email messages processed."""

    total_attachments: int = 0
    """Total number of attachments inspected across all emails."""

    total_discovered: int = 0
    """Total number of eligible PDF attachments discovered."""

    processed: int = 0
    """Total number of attachments dispatched through the pipeline."""

    successful: int = 0
    """Total number of documents successfully parsed (PARSED or PARSED_WITH_WARNINGS)."""

    failed: int = 0
    """Total number of documents that failed processing, extraction, or encryption without password."""

    duplicates: int = 0
    """Total number of documents identified as duplicates."""

    quarantined: int = 0
    """Total number of documents quarantined for manual review or learning."""

    skipped: int = 0
    """Total number of non-PDF or ineligible attachments skipped."""

    results: list[PipelineResult] = Field(default_factory=list)
    """Ordered per-attachment PipelineResult objects preserved from pipeline execution."""

    errors: list[str] = Field(default_factory=list)
    """Batch-level errors or diagnostic messages."""

    # Convenience alias properties
    @property
    def discovered_attachments(self) -> int:
        """Alias for total_discovered."""
        return self.total_discovered

    @property
    def discovered_files(self) -> int:
        """Alias for total_discovered."""
        return self.total_discovered

    @property
    def total_processed(self) -> int:
        """Alias for processed."""
        return self.processed

    @property
    def success_count(self) -> int:
        """Alias for successful."""
        return self.successful

    @property
    def failure_count(self) -> int:
        """Alias for failed."""
        return self.failed

    @property
    def skipped_count(self) -> int:
        """Alias for skipped."""
        return self.skipped


# Alias for callers who prefer the name EmailIngestionSummary
EmailIngestionSummary = EmailIngestionResult
