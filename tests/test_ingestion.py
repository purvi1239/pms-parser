"""Tests for Step 14: Folder Ingestion.

Covers:
1. Valid directory with multiple PDF files.
2. Empty directory handled gracefully.
3. Non-existent path reporting clear error.
4. Path pointing to a regular file reporting clear error.
5. Deterministic file ordering across discovery and processing.
6. Error isolation: one failing/crashing file does not halt the batch.
7. Encrypted file (NEEDS_PASSWORD) isolated and counted properly.
8. Correct aggregation of per-file results and summary counts (successful, failed, skipped, duplicates, quarantined).
9. Recursive versus non-recursive discovery behavior.
10. In-batch path deduplication (symlinks or repeated physical files).
11. Filtering of non-PDF files and case-insensitive extension matching.
12. Seamless interaction with the real Pipeline interface.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineConfig, PipelineResult
from src.pipeline.pipeline import Pipeline
from src.ingestion.exceptions import (
    DirectoryNotFoundError,
    IngestionError,
    InvalidDirectoryError,
    NotADirectoryPathError,
)
from src.ingestion.folder import (
    FolderIngester,
    FolderIngestion,
    PipelineInterface,
    ingest_folder,
)
from src.ingestion.models import (
    FolderIngestionConfig,
    FolderIngestionResult,
    FolderIngestionSummary,
)


def _make_result(
    file_path: str | Path,
    status: DocumentStatus = DocumentStatus.PARSED,
    is_duplicate: bool = False,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> PipelineResult:
    """Helper to create dummy PipelineResult objects for unit testing."""
    path_str = str(file_path)
    file_hash = hashlib.sha256(path_str.encode("utf-8")).hexdigest()
    return PipelineResult(
        document_id=f"doc_{file_hash[:12]}",
        status=status,
        file_path=path_str,
        file_hash=file_hash,
        is_duplicate=is_duplicate,
        errors=errors or [],
        warnings=warnings or [],
    )


class TestFolderIngestionPathValidation:
    """Tests path existence and directory verification."""

    def test_nonexistent_directory_raises_error(self, tmp_path: Path):
        """Passing a path that does not exist raises DirectoryNotFoundError."""
        missing_dir = tmp_path / "nonexistent_subfolder"
        mock_pipeline = MagicMock(spec=PipelineInterface)
        ingester = FolderIngester(pipeline=mock_pipeline)

        with pytest.raises(DirectoryNotFoundError) as exc_info:
            ingester.ingest(missing_dir)

        assert "does not exist" in str(exc_info.value).lower()
        # Verify it also satisfies InvalidDirectoryError, IngestionError, and FileNotFoundError
        assert isinstance(exc_info.value, InvalidDirectoryError)
        assert isinstance(exc_info.value, IngestionError)
        assert isinstance(exc_info.value, FileNotFoundError)

    def test_file_path_raises_not_a_directory(self, tmp_path: Path):
        """Passing a path to a regular file raises NotADirectoryPathError."""
        regular_file = tmp_path / "single_file.pdf"
        regular_file.write_bytes(b"%PDF-1.4 dummy")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        ingester = FolderIngester(pipeline=mock_pipeline)

        with pytest.raises(NotADirectoryPathError) as exc_info:
            ingester.ingest(regular_file)

        assert "not a directory" in str(exc_info.value).lower()
        # Verify it satisfies InvalidDirectoryError, IngestionError, and NotADirectoryError
        assert isinstance(exc_info.value, InvalidDirectoryError)
        assert isinstance(exc_info.value, IngestionError)
        assert isinstance(exc_info.value, NotADirectoryError)

    def test_empty_directory_handled_gracefully(self, tmp_path: Path):
        """An empty directory returns a valid result with zero totals and no exceptions."""
        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()

        mock_pipeline = MagicMock(spec=PipelineInterface)
        ingester = FolderIngester(pipeline=mock_pipeline)

        result = ingester.ingest(empty_dir)

        assert isinstance(result, FolderIngestionResult)
        assert result.total_discovered == 0
        assert result.processed == 0
        assert result.successful == 0
        assert result.failed == 0
        assert result.skipped == 0
        assert result.duplicates == 0
        assert result.quarantined == 0
        assert result.results == []
        mock_pipeline.process_file.assert_not_called()


class TestFolderIngestionDiscovery:
    """Tests file discovery, extension filtering, ordering, and recursion."""

    def test_deterministic_file_ordering(self, tmp_path: Path):
        """Files are discovered and processed in consistent alphabetical filename order."""
        files = ["zebra.pdf", "alpha.pdf", "charlie.pdf", "bravo.pdf"]
        for f in files:
            (tmp_path / f).write_bytes(b"%PDF-1.4 dummy")

        processed_order: list[str] = []

        def mock_process(file_path, password=None):
            p = Path(file_path)
            processed_order.append(p.name)
            return _make_result(p, DocumentStatus.PARSED)

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        expected_order = ["alpha.pdf", "bravo.pdf", "charlie.pdf", "zebra.pdf"]
        assert processed_order == expected_order
        assert [Path(r.file_path).name for r in result.results] == expected_order
        assert result.total_discovered == 4
        assert result.processed == 4
        assert result.successful == 4

    def test_non_recursive_by_default(self, tmp_path: Path):
        """Subdirectories are not scanned by default (recursive=False)."""
        (tmp_path / "top1.pdf").write_bytes(b"%PDF-1.4 dummy")
        (tmp_path / "top2.pdf").write_bytes(b"%PDF-1.4 dummy")

        sub_dir = tmp_path / "subfolder"
        sub_dir.mkdir()
        (sub_dir / "nested.pdf").write_bytes(b"%PDF-1.4 dummy")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 2
        names = {Path(r.file_path).name for r in result.results}
        assert names == {"top1.pdf", "top2.pdf"}

    def test_recursive_discovery(self, tmp_path: Path):
        """When recursive=True, discovers PDFs across all nested subdirectories."""
        (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4 dummy")

        sub1 = tmp_path / "branch1"
        sub1.mkdir()
        (sub1 / "b1_doc.pdf").write_bytes(b"%PDF-1.4 dummy")

        sub2 = tmp_path / "branch2" / "deep"
        sub2.mkdir(parents=True)
        (sub2 / "deep_doc.pdf").write_bytes(b"%PDF-1.4 dummy")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        # Configure recursive=True
        cfg = FolderIngestionConfig(recursive=True)
        ingester = FolderIngester(pipeline=mock_pipeline, config=cfg)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 3
        discovered_names = {Path(r.file_path).name for r in result.results}
        assert discovered_names == {"top.pdf", "b1_doc.pdf", "deep_doc.pdf"}

    def test_filters_non_pdf_files_and_case_insensitive(self, tmp_path: Path):
        """Only supported extensions are ingested, case-insensitively."""
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 pdf")
        (tmp_path / "UPPERCASE.PDF").write_bytes(b"%PDF-1.4 upper")
        (tmp_path / "MixedCase.Pdf").write_bytes(b"%PDF-1.4 mixed")
        (tmp_path / "readme.txt").write_text("not a pdf")
        (tmp_path / "data.csv").write_text("col1,col2")
        (tmp_path / "photo.png").write_bytes(b"PNG")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 3
        discovered_names = [Path(r.file_path).name for r in result.results]
        assert "statement.pdf" in discovered_names
        assert "UPPERCASE.PDF" in discovered_names
        assert "MixedCase.Pdf" in discovered_names

    def test_avoids_duplicate_processing_via_symlink(self, tmp_path: Path):
        """Symlinks pointing to the same file within a batch are not processed twice."""
        real_file = tmp_path / "original.pdf"
        real_file.write_bytes(b"%PDF-1.4 real")

        symlink_file = tmp_path / "symlink.pdf"
        try:
            symlink_file.symlink_to(real_file)
        except OSError:
            pytest.skip("Filesystem does not support symlinks")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 1
        assert result.processed == 1


class TestFolderIngestionFaultIsolationAndAggregation:
    """Tests error isolation and status aggregation."""

    def test_single_file_failure_does_not_halt_batch(self, tmp_path: Path):
        """When one file crashes or raises an exception, the remaining files are still processed."""
        f_good1 = tmp_path / "01_good.pdf"
        f_bad = tmp_path / "02_corrupt.pdf"
        f_good2 = tmp_path / "03_good.pdf"

        f_good1.write_bytes(b"%PDF-1.4 good")
        f_bad.write_bytes(b"%PDF-corrupt")
        f_good2.write_bytes(b"%PDF-1.4 good2")

        def mock_process(file_path, password=None):
            if "02_corrupt" in str(file_path):
                raise RuntimeError("Corrupt PDF stream")
            return _make_result(file_path, DocumentStatus.PARSED)

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 3
        assert result.processed == 3
        assert result.successful == 2
        assert result.failed == 1
        assert len(result.results) == 3

        # Check that the failed file result reflects FAILED status and contains the error message
        bad_res = result.results[1]
        assert bad_res.status == DocumentStatus.FAILED
        assert any("Corrupt PDF stream" in err for err in bad_res.errors)

        # Check good files
        assert result.results[0].status == DocumentStatus.PARSED
        assert result.results[2].status == DocumentStatus.PARSED

    def test_encrypted_pdf_needs_password_isolated(self, tmp_path: Path):
        """Encrypted PDF returning NEEDS_PASSWORD status is categorized as failed and does not block."""
        (tmp_path / "file_a.pdf").write_bytes(b"%PDF-1.4 a")
        (tmp_path / "file_b_locked.pdf").write_bytes(b"%PDF-1.4 locked")
        (tmp_path / "file_c.pdf").write_bytes(b"%PDF-1.4 c")

        def mock_process(file_path, password=None):
            if "locked" in str(file_path):
                return _make_result(
                    file_path,
                    status=DocumentStatus.NEEDS_PASSWORD,
                    errors=["Document encrypted; password required"],
                )
            return _make_result(file_path, status=DocumentStatus.PARSED)

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 3
        assert result.processed == 3
        assert result.successful == 2
        assert result.failed == 1
        assert result.results[1].status == DocumentStatus.NEEDS_PASSWORD

    def test_correct_aggregation_of_all_status_categories(self, tmp_path: Path):
        """Validates aggregation across successful, skipped, failed, duplicate, and quarantined files."""
        filenames = [
            "01_clean.pdf",
            "02_warn.pdf",
            "03_skipped.pdf",
            "04_duplicate.pdf",
            "05_drift_quarantine.pdf",
            "06_review_quarantine.pdf",
            "07_failed.pdf",
        ]
        for f in filenames:
            (tmp_path / f).write_bytes(b"%PDF-1.4 dummy")

        def mock_process(file_path, password=None):
            name = Path(file_path).name
            if name == "01_clean.pdf":
                return _make_result(file_path, status=DocumentStatus.PARSED)
            elif name == "02_warn.pdf":
                return _make_result(file_path, status=DocumentStatus.PARSED_WITH_WARNINGS)
            elif name == "03_skipped.pdf":
                return _make_result(file_path, status=DocumentStatus.SKIPPED)
            elif name == "04_duplicate.pdf":
                return _make_result(file_path, status=DocumentStatus.DUPLICATE, is_duplicate=True)
            elif name == "05_drift_quarantine.pdf":
                return _make_result(file_path, status=DocumentStatus.DRIFT_SUSPECTED)
            elif name == "06_review_quarantine.pdf":
                return _make_result(file_path, status=DocumentStatus.NEEDS_REVIEW)
            elif name == "07_failed.pdf":
                return _make_result(file_path, status=DocumentStatus.FAILED)
            raise ValueError(f"Unknown mock file: {name}")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = FolderIngester(pipeline=mock_pipeline)
        result = ingester.ingest(tmp_path)

        assert result.total_discovered == 7
        assert result.processed == 7
        assert result.successful == 2  # PARSED + PARSED_WITH_WARNINGS
        assert result.skipped == 1     # SKIPPED
        assert result.duplicates == 1  # DUPLICATE
        assert result.quarantined == 2 # DRIFT_SUSPECTED + NEEDS_REVIEW
        assert result.failed == 1      # FAILED


class TestFolderIngestionPublicApiAndAliases:
    """Tests the convenience function, class aliases, and model properties."""

    def test_ingest_folder_convenience_function(self, tmp_path: Path):
        """ingest_folder functional wrapper operates identically to FolderIngester."""
        (tmp_path / "doc1.pdf").write_bytes(b"%PDF-1.4 doc1")
        (tmp_path / "doc2.pdf").write_bytes(b"%PDF-1.4 doc2")

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        summary = ingest_folder(tmp_path, pipeline=mock_pipeline)

        assert isinstance(summary, FolderIngestionResult)
        assert summary.total_discovered == 2
        assert summary.successful == 2

    def test_class_and_model_aliases(self):
        """FolderIngestion aliases FolderIngester and FolderIngestionSummary aliases FolderIngestionResult."""
        assert FolderIngestion is FolderIngester
        assert FolderIngestionSummary is FolderIngestionResult

    def test_convenience_count_properties(self, tmp_path: Path):
        """Convenience properties on FolderIngestionResult return expected metrics."""
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.return_value = _make_result("doc.pdf", DocumentStatus.PARSED)

        res = FolderIngester(pipeline=mock_pipeline).ingest(tmp_path)
        assert res.discovered_files == 1
        assert res.total_processed == 1
        assert res.success_count == 1
        assert res.failure_count == 0
        assert res.skipped_count == 0

    def test_interaction_with_real_pipeline(self, tmp_path: Path):
        """FolderIngester successfully invokes an initialized Pipeline instance."""
        (tmp_path / "real.pdf").write_bytes(b"%PDF-1.4 real test")

        real_pipeline = Pipeline(config=PipelineConfig(store_results=False))
        # Patch extract_pdf to return a dummy IR or fail gracefully
        with patch.object(real_pipeline, "process_file") as mock_process:
            mock_process.return_value = _make_result(tmp_path / "real.pdf", DocumentStatus.PARSED)

            result = ingest_folder(tmp_path, pipeline=real_pipeline)

            assert result.total_discovered == 1
            assert result.successful == 1
            mock_process.assert_called_once_with(
                tmp_path / "real.pdf",
                password=None,
            )
