"""Tests for Step 16: Command-Line Interface (CLI).

Covers:
1. Help output (--help / -h) displays descriptions, options, and examples.
2. Missing arguments / invalid argument combinations return usage errors (exit code 2).
3. Non-existent file and directory paths return clear errors (exit code 1).
4. Unsupported file formats return clear error (exit code 1).
5. Passing a file path to --folder flag returns error (exit code 1).
6. Valid single-PDF invocation routes through pipeline and returns success (exit code 0).
7. Password argument is passed securely to pipeline without appearing in stdout/stderr.
8. Single-PDF pipeline failure returns error status and exit code 1.
9. Single-PDF pipeline exception is caught gracefully with error message (exit code 1).
10. Valid folder invocation routes through FolderIngester and reports summary (exit code 0).
11. Positional directory path automatically routes to folder processing.
12. Folder partial failure reports failed documents and returns exit code 1.
13. Recursive flag (--recursive / -r) is passed to folder ingestion.
14. Verbose flag (--verbose / -v) provides additional diagnostics.
15. main(argv=None) defaults to sys.argv correctly.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.cli.cli import (
    create_parser,
    main,
    process_folder,
    process_single_file,
)
from src.ingestion.folder import FolderIngester, PipelineInterface
from src.ingestion.models import FolderIngestionResult
from src.models.enums import DocumentStatus, StatementType
from src.pipeline.models import PipelineResult


def _make_result(
    file_path: str | Path,
    status: DocumentStatus = DocumentStatus.PARSED,
    is_duplicate: bool = False,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    statement_type: StatementType | None = None,
    format_id: str | None = None,
) -> PipelineResult:
    """Helper to create dummy PipelineResult objects for testing."""
    path_str = str(file_path)
    file_hash = hashlib.sha256(path_str.encode("utf-8")).hexdigest()
    return PipelineResult(
        document_id=f"doc_{file_hash[:12]}",
        status=status,
        file_path=path_str,
        file_hash=file_hash,
        statement_type=statement_type,
        format_id=format_id,
        is_duplicate=is_duplicate,
        errors=errors or [],
        warnings=warnings or [],
    )


class TestCliHelpAndArgumentValidation:
    """Tests argument parsing, help output, and validation errors."""

    def test_help_output(self):
        """CLI --help returns exit code 0 and displays descriptions and examples."""
        out = io.StringIO()
        err = io.StringIO()

        code = main(["--help"], out=out, err=err)

        assert code == 0
        help_text = out.getvalue()
        assert "pms-parser" in help_text
        assert "--folder" in help_text
        assert "--password" in help_text
        assert "--recursive" in help_text
        assert "--verbose" in help_text
        assert "Examples:" in help_text

    def test_no_arguments_returns_usage_error(self):
        """Calling CLI with no arguments prints an error message and returns exit code 2."""
        out = io.StringIO()
        err = io.StringIO()

        code = main([], out=out, err=err)

        assert code == 2
        err_text = err.getvalue()
        assert "Error: Please provide a PDF file path or --folder directory path." in err_text

    def test_both_positional_and_folder_conflicts(self):
        """Specifying both a positional path and --folder returns exit code 2."""
        out = io.StringIO()
        err = io.StringIO()

        code = main(["statement.pdf", "--folder", "./statements"], out=out, err=err)

        assert code == 2
        assert "Error: Cannot specify both a positional path and --folder." in err.getvalue()

    def test_nonexistent_file_path(self, tmp_path: Path):
        """Non-existent single file returns exit code 1 with clear error."""
        missing = tmp_path / "missing_statement.pdf"
        out = io.StringIO()
        err = io.StringIO()

        code = main([str(missing)], out=out, err=err)

        assert code == 1
        assert f"Error: Path does not exist: {missing}" in err.getvalue()

    def test_nonexistent_folder_path(self, tmp_path: Path):
        """Non-existent folder returns exit code 1 with clear error."""
        missing_dir = tmp_path / "nonexistent_dir"
        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(missing_dir)], out=out, err=err)

        assert code == 1
        assert f"Error: Directory does not exist: {missing_dir}" in err.getvalue()

    def test_unsupported_file_extension(self, tmp_path: Path):
        """Providing a non-PDF file returns exit code 1 with clear error."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("not a pdf")
        out = io.StringIO()
        err = io.StringIO()

        code = main([str(txt_file)], out=out, err=err)

        assert code == 1
        assert "Error: Unsupported file type: notes.txt. Only PDF files are supported." in err.getvalue()

    def test_folder_flag_pointing_to_regular_file(self, tmp_path: Path):
        """Providing a file path to --folder returns exit code 1."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 dummy")
        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(pdf_file)], out=out, err=err)

        assert code == 1
        assert f"Error: Path is not a directory: {pdf_file}" in err.getvalue()


class TestCliSingleFileProcessing:
    """Tests single PDF statement execution via CLI."""

    def test_valid_single_pdf_success(self, tmp_path: Path):
        """Valid PDF statement successfully processes and outputs status and metadata."""
        sample_pdf = tmp_path / "kotak_pms.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 sample")

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_result(
            sample_pdf,
            status=DocumentStatus.PARSED,
            statement_type=StatementType.PMS,
            format_id="kotak_pms_v1",
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main([str(sample_pdf)], pipeline=mock_pipe, out=out, err=err)

        assert code == 0
        output = out.getvalue()
        assert "File: kotak_pms.pdf" in output
        assert "Status: PARSED" in output
        assert "Statement Type: PMS" in output
        assert "Format ID: kotak_pms_v1" in output
        assert "Warnings: 0" in output
        assert "Errors: 0" in output
        mock_pipe.process_file.assert_called_once_with(sample_pdf, password=None)

    def test_single_pdf_with_password_security(self, tmp_path: Path):
        """Password is forwarded to pipeline but never logged or printed in output."""
        sample_pdf = tmp_path / "encrypted.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 encrypted")

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_result(sample_pdf, DocumentStatus.PARSED)

        out = io.StringIO()
        err = io.StringIO()

        code = main(
            [str(sample_pdf), "--password", "SuperSecretPassword123"],
            pipeline=mock_pipe,
            out=out,
            err=err,
        )

        assert code == 0
        mock_pipe.process_file.assert_called_once_with(sample_pdf, password="SuperSecretPassword123")
        # Ensure password is not leaked in stdout or stderr
        assert "SuperSecretPassword123" not in out.getvalue()
        assert "SuperSecretPassword123" not in err.getvalue()

    def test_single_pdf_pipeline_failure_status(self, tmp_path: Path):
        """Failed document parse returns exit code 1 and outputs error details."""
        sample_pdf = tmp_path / "corrupt.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 corrupt")

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_result(
            sample_pdf,
            status=DocumentStatus.FAILED,
            errors=["Failed to extract text from PDF: Corrupt byte stream"],
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main([str(sample_pdf)], pipeline=mock_pipe, out=out, err=err)

        assert code == 1
        output = out.getvalue()
        assert "File: corrupt.pdf" in output
        assert "Status: FAILED" in output
        assert "Errors: 1" in output
        assert "Corrupt byte stream" in output

    def test_single_pdf_pipeline_exception_handled(self, tmp_path: Path):
        """Unexpected exception during pipeline processing is caught and returns exit code 1."""
        sample_pdf = tmp_path / "crash.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 crash")

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.side_effect = RuntimeError("Fatal memory error")

        out = io.StringIO()
        err = io.StringIO()

        code = main([str(sample_pdf)], pipeline=mock_pipe, out=out, err=err)

        assert code == 1
        assert "Error: Pipeline execution failed on crash.pdf: Fatal memory error" in err.getvalue()


class TestCliFolderProcessing:
    """Tests folder ingestion execution via CLI."""

    def test_valid_folder_success(self, tmp_path: Path):
        """Folder ingestion displays summary counts and returns exit code 0 when all succeed."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.return_value = FolderIngestionResult(
            directory=str(tmp_path),
            total_discovered=3,
            processed=3,
            successful=3,
            duplicates=0,
            skipped=0,
            quarantined=0,
            failed=0,
            results=[
                _make_result(tmp_path / "stmt1.pdf"),
                _make_result(tmp_path / "stmt2.pdf"),
                _make_result(tmp_path / "stmt3.pdf"),
            ],
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(tmp_path)], ingester=mock_ingester, out=out, err=err)

        assert code == 0
        output = out.getvalue()
        assert f"Directory: {tmp_path}" in output
        assert "Discovered: 3" in output
        assert "Processed: 3" in output
        assert "Successful: 3" in output
        assert "Failed: 0" in output
        mock_ingester.ingest.assert_called_once_with(
            directory=tmp_path,
            recursive=False,
            password=None,
        )

    def test_positional_directory_routes_to_folder_ingestion(self, tmp_path: Path):
        """Passing a directory as a positional argument automatically routes to folder processing."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.return_value = FolderIngestionResult(
            directory=str(tmp_path),
            total_discovered=1,
            processed=1,
            successful=1,
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main([str(tmp_path)], ingester=mock_ingester, out=out, err=err)

        assert code == 0
        mock_ingester.ingest.assert_called_once_with(
            directory=tmp_path,
            recursive=False,
            password=None,
        )

    def test_folder_partial_failure_returns_exit_code_1(self, tmp_path: Path):
        """Folder with one or more failed documents reports failed files and returns exit code 1."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.return_value = FolderIngestionResult(
            directory=str(tmp_path),
            total_discovered=3,
            processed=3,
            successful=2,
            failed=1,
            results=[
                _make_result(tmp_path / "good1.pdf", DocumentStatus.PARSED),
                _make_result(
                    tmp_path / "bad.pdf",
                    DocumentStatus.FAILED,
                    errors=["Extraction failed: encrypted"],
                ),
                _make_result(tmp_path / "good2.pdf", DocumentStatus.PARSED),
            ],
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(tmp_path)], ingester=mock_ingester, out=out, err=err)

        assert code == 1
        output = out.getvalue()
        assert "Successful: 2" in output
        assert "Failed: 1" in output
        assert "Failed Documents (1):" in output
        assert "bad.pdf [FAILED]: Extraction failed: encrypted" in output

    def test_folder_recursive_flag(self, tmp_path: Path):
        """Passing --recursive / -r forwards recursive=True to the folder ingester."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.return_value = FolderIngestionResult(
            directory=str(tmp_path),
            total_discovered=2,
            processed=2,
            successful=2,
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main(
            ["--folder", str(tmp_path), "--recursive"],
            ingester=mock_ingester,
            out=out,
            err=err,
        )

        assert code == 0
        mock_ingester.ingest.assert_called_once_with(
            directory=tmp_path,
            recursive=True,
            password=None,
        )

    def test_folder_ingestion_exception_handled(self, tmp_path: Path):
        """Ingestion crash or unexpected error prints to stderr and returns exit code 1."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.side_effect = PermissionError("Permission denied on directory")

        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(tmp_path)], ingester=mock_ingester, out=out, err=err)

        assert code == 1
        assert "Error: Folder ingestion failed" in err.getvalue()
        assert "Permission denied" in err.getvalue()


class TestCliVerboseAndEntrypoint:
    """Tests verbose flag output and main entry point integration."""

    def test_verbose_single_file_output(self, tmp_path: Path):
        """Verbose mode adds Document ID, file hash, and classification details."""
        sample_pdf = tmp_path / "diag.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 diag")

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_result(sample_pdf, DocumentStatus.PARSED)

        out = io.StringIO()
        err = io.StringIO()

        code = main([str(sample_pdf), "--verbose"], pipeline=mock_pipe, out=out, err=err)

        assert code == 0
        output = out.getvalue()
        assert "Document ID:" in output
        assert "File Hash:" in output

    def test_verbose_folder_output(self, tmp_path: Path):
        """Verbose mode lists all processed documents in folder results."""
        mock_ingester = MagicMock(spec=FolderIngester)
        mock_ingester.ingest.return_value = FolderIngestionResult(
            directory=str(tmp_path),
            total_discovered=2,
            processed=2,
            successful=2,
            results=[
                _make_result(tmp_path / "f1.pdf", DocumentStatus.PARSED),
                _make_result(tmp_path / "f2.pdf", DocumentStatus.PARSED),
            ],
        )

        out = io.StringIO()
        err = io.StringIO()

        code = main(["--folder", str(tmp_path), "-v"], ingester=mock_ingester, out=out, err=err)

        assert code == 0
        output = out.getvalue()
        assert "Processed Documents (2):" in output
        assert "f1.pdf: PARSED" in output
        assert "f2.pdf: PARSED" in output

    def test_main_argv_none_uses_sys_argv(self):
        """Calling main() with argv=None reads arguments from sys.argv."""
        with patch("sys.argv", ["pms-parser", "--help"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main(argv=None)
                assert code == 0
                assert "pms-parser" in mock_out.getvalue()
