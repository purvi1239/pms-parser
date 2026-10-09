"""Data models for document classification."""
from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import StatementType


class ClassificationDecision(str, Enum):
    """Decision category for classified documents."""

    STATEMENT = "STATEMENT"  # Strong evidence of PMS or AIF financial statement
    MAYBE = "MAYBE"          # Ambiguous; routed to review queue
    SKIP = "SKIP"            # Clearly irrelevant or non-statement


class ClassificationResult(BaseModel):
    """Structured result of document classification."""

    model_config = ConfigDict(validate_assignment=True)

    decision: ClassificationDecision
    statement_type: StatementType | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    score: int
    reasons: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
