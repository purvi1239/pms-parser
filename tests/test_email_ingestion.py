"""Tests for Step 15: Email Ingestion.

Covers:
1. Valid PDF attachment successfully reaching the pipeline.
2. Multiple emails with multiple attachments.
3. Non-PDF attachments safely filtered out and skipped.
4. Missing filenames and empty attachments handled gracefully.
5. Invalid attachment data / PDF content handled without crashing.
6. One attachment failing without halting subsequent attachments or emails.
7. Pipeline exceptions isolated and recorded as failures.
8. Duplicate attachment handling (in-batch SHA-256 deduplication).
9. Correct aggregation of all status counts (discovered, processed, successful, failed, skipped, duplicates, quarantined).
10. Temporary file creation and reliable cleanup on both success and failure.
11. Standard library email.message.EmailMessage and raw .eml parsing.
12. Malformed email inputs handled gracefully.
13. Public API: EmailIngester, EmailIngestion alias, convenience functions ingest_emails and ingest_email_attachments.
14. Compatibility with the real Pipeline interface.
"""
from __future__ import annotations

from email.message import EmailMessage as StdlibEmailMessage
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineConfig, PipelineResult
from src.pipeline.pipeline import Pipeline
from src.ingestion.email import (
    EmailIngester,
    EmailIngestion,
    ingest_email_attachments,
    ingest_emails,
    parse_email_message,
)
from src.ingestion.exceptions import (
    EmailIngestionError,
    IngestionError,
    MalformedEmailError,
)
from src.ingestion.folder import PipelineInterface
from src.ingestion.models import (
    EmailAttachment,
    EmailIngestionConfig,
    EmailIngestionResult,
    EmailIngestionSummary,
    EmailMessage,
    EmailMessageInput,
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


class TestEmailIngestionBasics:
    """Core tests for processing email attachments through the Pipeline."""

    def test_valid_pdf_attachment_reaches_pipeline(self):
        """A valid PDF attachment is extracted, written to temp file, and processed by pipeline."""
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f, DocumentStatus.PARSED)

        ingester = EmailIngester(pipeline=mock_pipeline)
        attachment = EmailAttachment(
            filename="kotak_statement.pdf",
            content=b"%PDF-1.4 sample statement content",
        )

        result = ingester.ingest_attachments([attachment])

        assert isinstance(result, EmailIngestionResult)
        assert result.total_discovered == 1
        assert result.processed == 1
        assert result.successful == 1
        assert result.failed == 0
        assert result.skipped == 0
        assert len(result.results) == 1
        assert result.results[0].status == DocumentStatus.PARSED
        assert result.results[0].file_path == "kotak_statement.pdf"
        assert mock_pipeline.process_file.call_count == 1

    def test_multiple_emails_and_attachments(self):
        """Processes multiple emails with multiple attachments independently."""
        email1 = EmailMessageInput(
            email_id="msg_001",
            sender="pms1@fund.com",
            attachments=[
                EmailAttachment(filename="stmt1.pdf", content=b"%PDF-1.4 stmt 1"),
                EmailAttachment(filename="stmt2.pdf", content=b"%PDF-1.4 stmt 2"),
            ],
        )
        email2 = EmailMessageInput(
            email_id="msg_002",
            sender="pms2@fund.com",
            attachments=[
                EmailAttachment(filename="stmt3.pdf", content=b"%PDF-1.4 stmt 3"),
            ],
        )

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_emails([email1, email2])

        assert result.total_emails == 2
        assert result.total_attachments == 3
        assert result.total_discovered == 3
        assert result.processed == 3
        assert result.successful == 3
        assert mock_pipeline.process_file.call_count == 3


class TestEmailIngestionFilteringAndEdgeCases:
    """Tests filtering of non-PDFs, missing filenames, and empty attachments."""

    def test_non_pdf_attachments_skipped_safely(self):
        """Non-PDF attachments (e.g. .txt, .png, .csv) are filtered out and counted as skipped."""
        attachments = [
            EmailAttachment(filename="notes.txt", content=b"important notes"),
            EmailAttachment(filename="banner.PNG", content=b"PNG image bytes"),
            EmailAttachment(filename="data.csv", content=b"a,b,c"),
            EmailAttachment(filename="real_statement.pdf", content=b"%PDF-1.4 valid"),
        ]

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_attachments(attachments)

        assert result.total_attachments == 4
        assert result.total_discovered == 1
        assert result.skipped == 3
        assert result.processed == 1
        assert result.successful == 1
        # Pipeline called only once for the valid PDF
        assert mock_pipeline.process_file.call_count == 1

    def test_missing_filename_skipped_safely(self):
        """Attachments with missing or empty filename are safely skipped without error."""
        attachments = [
            EmailAttachment(filename=None, content=b"%PDF-1.4 missing name"),
            EmailAttachment(filename="", content=b"%PDF-1.4 empty name"),
            EmailAttachment(filename="valid.pdf", content=b"%PDF-1.4 valid"),
        ]

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_attachments(attachments)

        assert result.total_attachments == 3
        assert result.total_discovered == 1
        assert result.skipped == 2
        assert result.processed == 1
        assert result.successful == 1

    def test_empty_attachment_failed_without_crash(self):
        """An attachment with zero-byte content fails gracefully without crashing the batch."""
        attachments = [
            EmailAttachment(filename="empty.pdf", content=b""),
            EmailAttachment(filename="good.pdf", content=b"%PDF-1.4 good content"),
        ]

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_attachments(attachments)

        assert result.total_attachments == 2
        assert result.total_discovered == 2
        assert result.processed == 2
        assert result.successful == 1
        assert result.failed == 1
        # Check failed result properties
        assert result.results[0].status == DocumentStatus.FAILED
        assert any("empty" in err.lower() for err in result.results[0].errors)
        # Check that good.pdf was still processed
        assert result.results[1].status == DocumentStatus.PARSED


class TestEmailIngestionFaultIsolationAndDuplicates:
    """Tests failure isolation, exception handling, and deduplication."""

    def test_one_attachment_failing_does_not_halt_batch(self):
        """When pipeline raises an exception on one attachment, other attachments proceed normally."""
        attachments = [
            EmailAttachment(filename="01_good.pdf", content=b"%PDF-1.4 doc 1"),
            EmailAttachment(filename="02_crash.pdf", content=b"%PDF-1.4 corrupt byte stream"),
            EmailAttachment(filename="03_good.pdf", content=b"%PDF-1.4 doc 3"),
        ]

        def mock_process(path, password=None):
            # Check temp file content or name
            content = Path(path).read_bytes()
            if b"corrupt byte stream" in content:
                raise RuntimeError("PDF stream decompression failure")
            return _make_result(path, DocumentStatus.PARSED)

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_attachments(attachments)

        assert result.total_discovered == 3
        assert result.processed == 3
        assert result.successful == 2
        assert result.failed == 1
        assert result.results[1].status == DocumentStatus.FAILED
        assert any("decompression failure" in err for err in result.results[1].errors)
        # Doc 3 was successfully processed
        assert result.results[2].status == DocumentStatus.PARSED

    def test_duplicate_attachment_deduplication(self):
        """Identical attachment content within a batch is identified as DUPLICATE."""
        identical_bytes = b"%PDF-1.4 identical content repeated"
        attachments = [
            EmailAttachment(filename="statement_copy1.pdf", content=identical_bytes),
            EmailAttachment(filename="statement_copy2.pdf", content=identical_bytes),
        ]

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f, DocumentStatus.PARSED)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_attachments(attachments)

        assert result.total_discovered == 2
        assert result.processed == 2
        assert result.successful == 1
        assert result.duplicates == 1
        # The second result is marked DUPLICATE
        assert result.results[1].is_duplicate is True
        assert result.results[1].status == DocumentStatus.DUPLICATE
        # Pipeline was called only once because duplicate was deduplicated in-batch
        assert mock_pipeline.process_file.call_count == 1

    def test_temporary_file_cleanup_on_success_and_failure(self):
        """Temporary files are guaranteed to be cleaned up after pipeline execution, even on failure."""
        captured_paths: list[Path] = []

        def mock_process(path, password=None):
            p = Path(path)
            captured_paths.append(p)
            assert p.exists(), "Temp file must exist during pipeline processing"
            if "fail" in p.name or "bad" in str(p):
                raise RuntimeError("Extraction failed")
            return _make_result(p, DocumentStatus.PARSED)

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = mock_process

        ingester = EmailIngester(pipeline=mock_pipeline)
        attachments = [
            EmailAttachment(filename="success.pdf", content=b"%PDF-1.4 success"),
            EmailAttachment(filename="fail.pdf", content=b"%PDF-1.4 fail"),
        ]

        ingester.ingest_attachments(attachments)

        assert len(captured_paths) == 2
        for p in captured_paths:
            assert not p.exists(), f"Temporary file {p} should have been unlinked after processing"


class TestEmailIngestionFormatsAndParsing:
    """Tests parsing stdlib EmailMessage, raw .eml bytes, and malformed emails."""

    def test_stdlib_email_message_parsing(self):
        """Extracts attachments from Python's standard library EmailMessage object."""
        msg = StdlibEmailMessage()
        msg["From"] = "advisor@wealth.com"
        msg["Subject"] = "Quarterly Valuation"
        msg["Message-ID"] = "<test-123@wealth.com>"
        msg.set_content("Please find attached your statement.")

        # Attach a PDF statement
        msg.add_attachment(
            b"%PDF-1.4 stdlib statement",
            maintype="application",
            subtype="pdf",
            filename="q1_statement.pdf",
        )
        # Attach a non-PDF image
        msg.add_attachment(
            b"GIF89a dummy image",
            maintype="image",
            subtype="gif",
            filename="logo.gif",
        )

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_email(msg)

        assert result.total_emails == 1
        assert result.total_attachments == 2
        assert result.total_discovered == 1  # only PDF
        assert result.skipped == 1           # logo.gif
        assert result.successful == 1
        assert result.results[0].file_path == "q1_statement.pdf"

    def test_raw_eml_bytes_parsing(self):
        """Parses raw RFC-822 formatted .eml byte strings."""
        raw_eml = (
            b"From: test@fund.com\r\n"
            b"Subject: Statement Email\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"BOUNDARY\"\r\n"
            b"\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Hello, here is your statement.\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=\"oct_statement.pdf\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"\r\n"
            b"JVBERi0xLjQgZHVtbXk=\r\n"
            b"--BOUNDARY--\r\n"
        )

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        ingester = EmailIngester(pipeline=mock_pipeline)
        result = ingester.ingest_email(raw_eml)

        assert result.total_emails == 1
        assert result.total_discovered == 1
        assert result.successful == 1
        assert result.results[0].file_path == "oct_statement.pdf"

    def test_malformed_email_handled_gracefully(self):
        """Malformed email input records an error without halting the batch."""
        mock_pipeline = MagicMock(spec=PipelineInterface)
        ingester = EmailIngester(pipeline=mock_pipeline)

        # Pass an invalid object that cannot be parsed as an email
        result = ingester.ingest_emails([12345])

        assert result.total_emails == 1
        assert len(result.errors) >= 1
        assert any("Failed to parse" in err for err in result.errors)
        assert result.total_discovered == 0


class TestEmailIngestionPublicApiAndAliases:
    """Tests convenience functions, class aliases, and model properties."""

    def test_convenience_functions(self):
        """Tests ingest_emails and ingest_email_attachments convenience functions."""
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_result(f)

        att = EmailAttachment(filename="direct.pdf", content=b"%PDF-1.4 direct")
        res1 = ingest_email_attachments([att], pipeline=mock_pipeline)
        assert res1.total_discovered == 1
        assert res1.successful == 1

        email_obj = EmailMessageInput(email_id="e1", attachments=[att])
        res2 = ingest_emails([email_obj], pipeline=mock_pipeline)
        assert res2.total_discovered == 1
        assert res2.successful == 1

    def test_aliases_and_properties(self):
        """Tests class aliases and summary model properties."""
        assert EmailIngestion is EmailIngester
        assert EmailIngestionSummary is EmailIngestionResult
        assert EmailMessage is EmailMessageInput

        res = EmailIngestionResult(
            total_discovered=5,
            processed=4,
            successful=2,
            failed=1,
            skipped=3,
        )
        assert res.discovered_attachments == 5
        assert res.discovered_files == 5
        assert res.total_processed == 4
        assert res.success_count == 2
        assert res.failure_count == 1
        assert res.skipped_count == 3

    def test_interaction_with_real_pipeline(self, tmp_path: Path):
        """EmailIngester successfully invokes an initialized Pipeline instance."""
        real_pipeline = Pipeline(config=PipelineConfig(store_results=False))
        with patch.object(real_pipeline, "process_file") as mock_process:
            mock_process.return_value = _make_result("real_test.pdf", DocumentStatus.PARSED)

            ingester = EmailIngester(pipeline=real_pipeline)
            res = ingester.ingest_attachments([
                EmailAttachment(filename="real_test.pdf", content=b"%PDF-1.4 test"),
            ])

            assert res.total_discovered == 1
            assert res.successful == 1
            assert mock_process.call_count == 1
