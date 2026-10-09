"""Domain models for structural document fingerprinting."""
from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class FingerprintMatchStatus(str, Enum):
    """Classification status when comparing against known format fingerprints."""

    KNOWN = "KNOWN"                      # Matches an existing format spec (score >= match_threshold)
    DRIFT_SUSPECTED = "DRIFT_SUSPECTED"  # Structurally near match; potential template drift
    UNKNOWN = "UNKNOWN"                  # No matching format template found


class LayoutStats(BaseModel):
    """High-level geometric and structural layout statistics."""

    model_config = ConfigDict(validate_assignment=True)

    page_count: int = 0
    table_count: int = 0
    column_counts: list[int] = Field(default_factory=list)


class FormatFingerprint(BaseModel):
    """Structural feature set extracted from a DocumentIR for fingerprint comparison."""

    model_config = ConfigDict(validate_assignment=True)

    format_id: str | None = None
    anchor_phrases: list[str] = Field(default_factory=list)
    table_headers: list[list[str]] = Field(default_factory=list)
    layout_stats: LayoutStats = Field(default_factory=LayoutStats)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FingerprintMatchResult(BaseModel):
    """Result of fingerprint comparison."""

    model_config = ConfigDict(validate_assignment=True)

    status: FingerprintMatchStatus
    similarity_score: float = Field(ge=0.0, le=1.0)
    matched_format_id: str | None = None
    component_scores: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
