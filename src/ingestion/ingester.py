"""Re-export folder ingestion classes and utilities."""
from src.ingestion.folder import (
    FolderIngester,
    FolderIngestion,
    PipelineInterface,
    ingest_folder,
)

__all__ = [
    "FolderIngester",
    "FolderIngestion",
    "PipelineInterface",
    "ingest_folder",
]
