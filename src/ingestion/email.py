"""Email ingestion module for discovering and processing statement PDF attachments."""
from __future__ import annotations

import email
from email.message import Message
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol, Sequence, runtime_checkable
import uuid

from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineResult
from src.ingestion.exceptions import MalformedEmailError
from src.ingestion.folder import PipelineInterface
from src.ingestion.models import (
    EmailAttachment,
    EmailIngestionConfig,
    EmailIngestionResult,
    EmailIngestionSummary,
    EmailMessage,
    EmailMessageInput,
)


def parse_email_message(email_input: Any) -> EmailMessageInput:
    """Parse various email representations into a standardized EmailMessageInput.

    Supports:
    - EmailMessageInput / EmailMessage instances
    - Standard library email.message.Message or EmailMessage
    - Raw bytes or string containing .eml RFC-822 formatted messages
    - Path or file path string pointing to a .eml file on disk
    - Dictionaries matching EmailMessageInput fields
    """
    if isinstance(email_input, EmailMessageInput):
        return email_input

    if isinstance(email_input, dict):
        # Convert raw attachment dicts to EmailAttachment objects if needed
        raw_atts = email_input.get("attachments", [])
        atts = [
            a if isinstance(a, EmailAttachment) else EmailAttachment(**a)
            for a in raw_atts
        ]
        return EmailMessageInput(
            email_id=email_input.get("email_id"),
            sender=email_input.get("sender"),
            subject=email_input.get("subject"),
            attachments=atts,
        )

    # Path to .eml file
    if isinstance(email_input, Path) or (isinstance(email_input, str) and "\n" not in email_input and len(email_input) < 1024):
        p = Path(email_input)
        if p.is_file():
            try:
                raw_bytes = p.read_bytes()
                msg = email.message_from_bytes(raw_bytes)
                return _extract_from_stdlib_message(msg)
            except Exception as exc:
                raise MalformedEmailError(f"Failed to read .eml file {p}: {exc}") from exc

    # Raw bytes
    if isinstance(email_input, bytes):
        try:
            msg = email.message_from_bytes(email_input)
            return _extract_from_stdlib_message(msg)
        except Exception as exc:
            raise MalformedEmailError(f"Failed to parse raw email bytes: {exc}") from exc

    # String .eml content
    if isinstance(email_input, str):
        try:
            msg = email.message_from_string(email_input)
            return _extract_from_stdlib_message(msg)
        except Exception as exc:
            raise MalformedEmailError(f"Failed to parse email string: {exc}") from exc

    # Standard library email message
    if isinstance(email_input, Message):
        return _extract_from_stdlib_message(email_input)

    raise MalformedEmailError(f"Unsupported email input type: {type(email_input)}")


def _extract_from_stdlib_message(msg: Message) -> EmailMessageInput:
    """Extract metadata and attachments from a stdlib email.message.Message."""
    email_id = msg.get("Message-ID")
    sender = msg.get("From")
    subject = msg.get("Subject")

    attachments: list[EmailAttachment] = []

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", "")).lower()
        filename = part.get_filename()
        content_type = str(part.get_content_type()).lower()

        # Check if the part represents an attachment
        is_attachment = bool(filename) or "attachment" in content_disposition
        if is_attachment:
            payload = part.get_payload(decode=True)
            if payload is not None:
                attachments.append(
                    EmailAttachment(
                        filename=filename,
                        content=payload,
                        content_type=content_type,
                    )
                )

    return EmailMessageInput(
        email_id=email_id,
        sender=sender,
        subject=subject,
        attachments=attachments,
    )


class EmailIngester:
    """Ingests email attachments and passes eligible PDF statements through the Pipeline."""

    def __init__(
        self,
        pipeline: PipelineInterface,
        config: EmailIngestionConfig | None = None,
    ) -> None:
        """Initialize EmailIngester with a pipeline and optional configuration.

        Args:
            pipeline: Pipeline instance or PipelineInterface implementation.
            config: Email ingestion configuration parameters.
        """
        self.pipeline = pipeline
        self.config = config or EmailIngestionConfig()

    def ingest_emails(
        self,
        emails: Sequence[Any],
        password: str | None = None,
    ) -> EmailIngestionResult:
        """Process a collection of emails and their statement attachments.

        Args:
            emails: Sequence of email representations (EmailMessageInput, stdlib Message, bytes, or dict).
            password: Optional explicit password for encrypted attachments.

        Returns:
            EmailIngestionResult summarizing discovery, counts, and per-attachment PipelineResults.
        """
        result_summary = EmailIngestionResult()
        seen_hashes: set[str] = set()

        for raw_email in emails:
            result_summary.total_emails += 1
            try:
                email_msg = parse_email_message(raw_email)
            except Exception as exc:
                result_summary.errors.append(f"Failed to parse email message: {exc}")
                continue

            for attachment in email_msg.attachments:
                result_summary.total_attachments += 1

                # 1. Check filename and PDF extension
                filename = attachment.filename
                if not filename:
                    # Missing filename -> safely skip
                    result_summary.skipped += 1
                    continue

                ext = Path(filename).suffix.lower()
                allowed_exts = {
                    e.lower() if e.startswith(".") else f".{e.lower()}"
                    for e in self.config.supported_extensions
                }
                if ext not in allowed_exts:
                    # Non-PDF attachment -> safely skip
                    result_summary.skipped += 1
                    continue

                # Discovered an eligible statement PDF attachment
                result_summary.total_discovered += 1

                # 2. Check for empty attachment content
                content = attachment.content
                if not content:
                    res = PipelineResult(
                        document_id=f"err_{uuid.uuid4().hex[:12]}",
                        status=DocumentStatus.FAILED,
                        file_path=filename,
                        file_hash="",
                        errors=["Attachment content is empty"],
                    )
                    result_summary.results.append(res)
                    result_summary.processed += 1
                    result_summary.failed += 1
                    continue

                # 3. Deduplicate identical attachment content hashes within the batch
                content_hash = hashlib.sha256(content).hexdigest()
                if self.config.deduplicate_batch and content_hash in seen_hashes:
                    res = PipelineResult(
                        document_id=f"doc_{content_hash[:12]}",
                        status=DocumentStatus.DUPLICATE,
                        file_path=filename,
                        file_hash=content_hash,
                        is_duplicate=True,
                        warnings=["Duplicate attachment content hash already seen in this batch."],
                    )
                    result_summary.results.append(res)
                    result_summary.processed += 1
                    result_summary.duplicates += 1
                    continue

                seen_hashes.add(content_hash)

                # 4. Safe temporary file processing with guaranteed cleanup
                tmp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
                        tmp_path = Path(tmp_file.name)
                        tmp_file.write(content)

                    pwd = password
                    if pwd is None and self.config.passwords:
                        pwd = self.config.passwords[0]

                    try:
                        # Invoke pipeline with positional file_path
                        res = self.pipeline.process_file(tmp_path, password=pwd)
                        # Preserve original attachment filename in result
                        res.file_path = filename
                    except Exception as exc:
                        res = PipelineResult(
                            document_id=f"err_{uuid.uuid4().hex[:12]}",
                            status=DocumentStatus.FAILED,
                            file_path=filename,
                            file_hash=content_hash,
                            errors=[f"Pipeline execution failure on attachment: {exc}"],
                        )

                    result_summary.results.append(res)
                    result_summary.processed += 1

                    # Aggregate result counts
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

                finally:
                    # Reliably clean up the temporary file
                    if tmp_path is not None:
                        try:
                            if tmp_path.exists():
                                tmp_path.unlink()
                        except OSError:
                            pass

        return result_summary

    def ingest_email(
        self,
        email: Any,
        password: str | None = None,
    ) -> EmailIngestionResult:
        """Process a single email message and its attachments.

        Args:
            email: Single email representation.
            password: Optional explicit password for encrypted attachments.

        Returns:
            EmailIngestionResult for the email.
        """
        return self.ingest_emails([email], password=password)

    def ingest_attachments(
        self,
        attachments: Sequence[EmailAttachment | dict],
        password: str | None = None,
    ) -> EmailIngestionResult:
        """Process a collection of attachments directly without an explicit email container.

        Args:
            attachments: Sequence of EmailAttachment objects or attachment dictionaries.
            password: Optional explicit password for encrypted attachments.

        Returns:
            EmailIngestionResult summarizing attachment outcomes.
        """
        atts = [
            a if isinstance(a, EmailAttachment) else EmailAttachment(**a)
            for a in attachments
        ]
        virtual_email = EmailMessageInput(
            email_id="direct_attachments",
            attachments=atts,
        )
        return self.ingest_emails([virtual_email], password=password)

    def ingest(
        self,
        source: Any,
        password: str | None = None,
    ) -> EmailIngestionResult:
        """Polymorphic entry point accepting emails, attachment lists, or single items.

        Args:
            source: Sequence of emails, sequence of attachments, single email, or single attachment.
            password: Optional explicit password.

        Returns:
            EmailIngestionResult.
        """
        if isinstance(source, Sequence) and not isinstance(source, (bytes, str)):
            if source and isinstance(source[0], EmailAttachment):
                return self.ingest_attachments(source, password=password)
            return self.ingest_emails(source, password=password)
        elif isinstance(source, EmailAttachment):
            return self.ingest_attachments([source], password=password)
        else:
            return self.ingest_email(source, password=password)


# Alias for callers expecting EmailIngestion
EmailIngestion = EmailIngester


def ingest_emails(
    emails: Sequence[Any],
    pipeline: PipelineInterface,
    config: EmailIngestionConfig | None = None,
    password: str | None = None,
) -> EmailIngestionResult:
    """Convenience function to ingest multiple emails using a Pipeline.

    Args:
        emails: Sequence of emails.
        pipeline: Pipeline or PipelineInterface implementation.
        config: Optional EmailIngestionConfig.
        password: Optional explicit password.

    Returns:
        EmailIngestionResult.
    """
    ingester = EmailIngester(pipeline=pipeline, config=config)
    return ingester.ingest_emails(emails, password=password)


def ingest_email_attachments(
    attachments: Sequence[Any],
    pipeline: PipelineInterface,
    config: EmailIngestionConfig | None = None,
    password: str | None = None,
) -> EmailIngestionResult:
    """Convenience function to ingest a list of email attachments using a Pipeline.

    Args:
        attachments: Sequence of EmailAttachment objects or dictionaries.
        pipeline: Pipeline or PipelineInterface implementation.
        config: Optional EmailIngestionConfig.
        password: Optional explicit password.

    Returns:
        EmailIngestionResult.
    """
    ingester = EmailIngester(pipeline=pipeline, config=config)
    return ingester.ingest_attachments(attachments, password=password)
