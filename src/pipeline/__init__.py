"""Pipeline integration module orchestrating end-to-end document processing."""
from src.pipeline.exceptions import (
    DocumentExtractionError,
    DriftDetectedPipelineError,
    PasswordRequiredError,
    PipelineError,
    PipelineStorageError,
    UnsupportedFormatPipelineError,
)
from src.pipeline.models import (
    BatchPipelineResult,
    PipelineConfig,
    PipelineResult,
)
from src.pipeline.pipeline import (
    Pipeline,
    compute_file_hash,
    compute_ir_hash,
)

__all__ = [
    "Pipeline",
    "PipelineConfig",
    "PipelineResult",
    "BatchPipelineResult",
    "compute_file_hash",
    "compute_ir_hash",
    "PipelineError",
    "DocumentExtractionError",
    "PasswordRequiredError",
    "UnsupportedFormatPipelineError",
    "DriftDetectedPipelineError",
    "PipelineStorageError",
]
