"""Folder ingestion module for discovering and processing PDF statement files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable
import uuid

from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineResult
from src.ingestion.exceptions import (
    DirectoryNotFoundError,
    InvalidDirectoryError,
    NotADirectoryPathError,
)
from src.ingestion.models import (
    FolderIngestionConfig,
    FolderIngestionResult,
    FolderIngestionSummary,
)


@runtime_checkable
class PipelineInterface(Protocol):
    """Protocol representing the required Pipeline interface for processing files."""

    def process_file(
        self,
        file_path: str | Path,
        password: str | None = None,
    ) -> PipelineResult:
        ...


class FolderIngester:
    """Ingester that discovers supported PDF files in a directory and processes them via a Pipeline."""

    def __init__(
        self,
        pipeline: PipelineInterface,
        config: FolderIngestionConfig | None = None,
    ) -> None:
        """Initialize FolderIngester with a pipeline and optional configuration.

        Args:
            pipeline: Pipeline instance or compatible object providing process_file.
            config: Folder ingestion configuration settings.
        """
        self.pipeline = pipeline
        self.config = config or FolderIngestionConfig()

    def discover_files(
        self,
        directory: str | Path,
        recursive: bool | None = None,
    ) -> list[Path]:
        """Discover supported PDF files deterministically in the given directory.

        Args:
            directory: Path to the target directory.
            recursive: Override for recursive search. If None, uses self.config.recursive.

        Returns:
            Sorted, deduplicated list of Path objects for discovered files.

        Raises:
            DirectoryNotFoundError: If directory does not exist.
            NotADirectoryPathError: If directory exists but is not a directory.
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            raise DirectoryNotFoundError(f"Ingestion directory does not exist: {dir_path}")
        if not dir_path.is_dir():
            raise NotADirectoryPathError(f"Ingestion path is not a directory: {dir_path}")

        is_recursive = self.config.recursive if recursive is None else recursive

        # Normalize allowed extensions: lowercase with leading dot
        allowed_exts = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in self.config.file_extensions
        }

        discovered: list[Path] = []
        if is_recursive:
            for root, dirs, files in os.walk(dir_path):
                dirs.sort()  # deterministic walk order
                for f in sorted(files):
                    p = Path(root) / f
                    if p.is_file() and p.suffix.lower() in allowed_exts:
                        discovered.append(p)
        else:
            for item in sorted(dir_path.iterdir(), key=lambda p: (p.name, str(p))):
                if item.is_file() and item.suffix.lower() in allowed_exts:
                    discovered.append(item)

        # Sort deterministically by filename, then full path
        discovered.sort(key=lambda p: (p.name, str(p)))

        # Deduplicate files within single batch (e.g., symlinks pointing to same physical file)
        seen_canonical: set[Path] = set()
        unique_files: list[Path] = []
        for p in discovered:
            try:
                canonical = p.resolve()
            except OSError:
                canonical = p
            if canonical not in seen_canonical:
                seen_canonical.add(canonical)
                unique_files.append(p)

        return unique_files

    def ingest(
        self,
        directory: str | Path,
        recursive: bool | None = None,
        password: str | None = None,
    ) -> FolderIngestionResult:
        """Process all supported files in the target directory through the pipeline.

        Args:
            directory: Path to the directory containing statement files.
            recursive: Optional override for recursive subdirectory scanning.
            password: Optional explicit password for encrypted documents in this batch.

        Returns:
            FolderIngestionResult containing discovery counts, per-file results, and category totals.

        Raises:
            DirectoryNotFoundError: If directory does not exist.
            NotADirectoryPathError: If path exists but is not a directory.
        """
        dir_path = Path(directory)
        files = self.discover_files(dir_path, recursive=recursive)

        result_summary = FolderIngestionResult(
            directory=str(dir_path),
            total_discovered=len(files),
        )

        # Handle empty directory gracefully
        if not files:
            return result_summary

        # Process files one at a time, isolating per-file failures
        for file_path in files:
            pwd = password
            if pwd is None and self.config.passwords:
                pwd = self.config.passwords[0]

            try:
                res = self.pipeline.process_file(file_path, password=pwd)
            except Exception as exc:
                res = PipelineResult(
                    document_id=f"err_{uuid.uuid4().hex[:12]}",
                    status=DocumentStatus.FAILED,
                    file_path=str(file_path),
                    file_hash="",
                    errors=[f"Ingestion pipeline execution failure: {exc}"],
                )

            result_summary.results.append(res)
            result_summary.processed += 1

            if res.is_duplicate or res.status == DocumentStatus.DUPLICATE:
                result_summary.duplicates += 1
            elif res.status in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS):
                result_summary.successful += 1
            elif res.status in (DocumentStatus.SKIPPED,):
                result_summary.skipped += 1
            elif res.is_quarantined:
                result_summary.quarantined += 1
            else:
                result_summary.failed += 1

        return result_summary

    # Convenient alias for callers expecting process_directory
    process_directory = ingest


# Alias for callers expecting FolderIngestion
FolderIngestion = FolderIngester


def ingest_folder(
    directory: str | Path,
    pipeline: PipelineInterface,
    recursive: bool = False,
    config: FolderIngestionConfig | None = None,
    password: str | None = None,
) -> FolderIngestionResult:
    """Convenience function to ingest a folder of PDF files using a Pipeline.

    Args:
        directory: Directory path to ingest.
        pipeline: Pipeline instance or PipelineInterface implementation.
        recursive: Whether to scan subdirectories recursively. Defaults to False.
        config: Optional FolderIngestionConfig.
        password: Optional explicit password for encrypted PDFs.

    Returns:
        FolderIngestionResult with batch statistics and per-file results.
    """
    cfg = config or FolderIngestionConfig(recursive=recursive)
    if recursive is not False and config is None:
        cfg.recursive = recursive
    ingester = FolderIngester(pipeline=pipeline, config=cfg)
    return ingester.ingest(directory=directory, recursive=recursive, password=password)
