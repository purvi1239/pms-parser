"""Ingestion modules for folder and email statement processing."""
from src.ingestion.email import (
    EmailIngester,
    EmailIngestion,
    ingest_email_attachments,
    ingest_emails,
    parse_email_message,
)
from src.ingestion.exceptions import (
    DirectoryNotFoundError,
    EmailIngestionError,
    IngestionError,
    InvalidDirectoryError,
    MalformedEmailError,
    NotADirectoryPathError,
)
from src.ingestion.folder import (
    FolderIngester,
    FolderIngestion,
    PipelineInterface,
    ingest_folder,
)
from src.ingestion.models import (
    EmailAttachment,
    EmailIngestionConfig,
    EmailIngestionResult,
    EmailIngestionSummary,
    EmailMessage,
    EmailMessageInput,
    FolderIngestionConfig,
    FolderIngestionResult,
    FolderIngestionSummary,
)

__all__ = [
    # Folder Ingestion
    "FolderIngester",
    "FolderIngestion",
    "FolderIngestionConfig",
    "FolderIngestionResult",
    "FolderIngestionSummary",
    "ingest_folder",
    "PipelineInterface",
    # Email Ingestion
    "EmailIngester",
    "EmailIngestion",
    "EmailIngestionConfig",
    "EmailIngestionResult",
    "EmailIngestionSummary",
    "EmailAttachment",
    "EmailMessage",
    "EmailMessageInput",
    "ingest_emails",
    "ingest_email_attachments",
    "parse_email_message",
    # Exceptions
    "IngestionError",
    "InvalidDirectoryError",
    "DirectoryNotFoundError",
    "NotADirectoryPathError",
    "EmailIngestionError",
    "MalformedEmailError",
]
