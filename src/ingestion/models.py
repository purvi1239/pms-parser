"""Data models and configuration for the folder ingestion module."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

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
