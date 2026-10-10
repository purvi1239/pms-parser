"""Data models for redaction configuration, items, and results.

All models strictly exclude original sensitive values to guarantee
that PII is never retained, logged, or serialized into metadata.
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class EntityType(str, Enum):
    """Types of sensitive entities recognized by the redactor."""

    EMAIL = "email"
    PHONE = "phone"
    PAN = "pan"
    AADHAAR = "aadhaar"
    BANK_ACCOUNT = "bank_account"
    DEMAT = "demat"
    NAME = "name"
    CUSTOM = "custom"


class RedactionConfig(BaseModel):
    """Configuration options for redaction patterns and replacement tokens."""

    model_config = ConfigDict(validate_assignment=True)

    redact_email: bool = True
    redact_phone: bool = True
    redact_pan: bool = True
    redact_aadhaar: bool = True
    redact_bank_account: bool = True
    redact_demat: bool = True
    redact_names: bool = True

    # Callers can provide known investor names, client IDs, or account tokens
    custom_sensitive_values: list[str] = Field(default_factory=list)

    # When True, tokens are indexed per unique value (e.g. [REDACTED_EMAIL_1])
    # When False, standard tokens are used (e.g. [REDACTED_EMAIL])
    use_indexed_tokens: bool = False

    # Token template mapping
    token_templates: dict[EntityType, str] = Field(
        default_factory=lambda: {
            EntityType.EMAIL: "[REDACTED_EMAIL]",
            EntityType.PHONE: "[REDACTED_PHONE]",
            EntityType.PAN: "[REDACTED_PAN]",
            EntityType.AADHAAR: "[REDACTED_AADHAAR]",
            EntityType.BANK_ACCOUNT: "[REDACTED_BANK_ACCOUNT]",
            EntityType.DEMAT: "[REDACTED_DEMAT_ACCOUNT]",
            EntityType.NAME: "[REDACTED_NAME]",
            EntityType.CUSTOM: "[REDACTED_CUSTOM]",
        }
    )


class RedactionItem(BaseModel):
    """Metadata for a single redacted occurrence.

    NOTE: The raw sensitive value is intentionally NEVER stored in this model.
    Only non-sensitive metadata (type, replacement token, offsets, length, and hash)
    is recorded for audit and verification purposes.
    """

    model_config = ConfigDict(validate_assignment=True)

    entity_type: EntityType
    token: str
    start_char: int
    end_char: int
    value_length: int
    value_hash: str = Field(
        ...,
        description="Truncated SHA-256 hash of the sensitive value for correlation without plaintext storage.",
    )

    @classmethod
    def from_match(
        cls,
        entity_type: EntityType,
        token: str,
        start_char: int,
        end_char: int,
        raw_val: str,
    ) -> RedactionItem:
        """Create a RedactionItem without retaining the raw sensitive value."""
        v_hash = hashlib.sha256(raw_val.strip().lower().encode("utf-8")).hexdigest()[:12]
        return cls(
            entity_type=entity_type,
            token=token,
            start_char=start_char,
            end_char=end_char,
            value_length=len(raw_val),
            value_hash=v_hash,
        )


class RedactionResult(BaseModel):
    """Result of a text redaction operation containing sanitized text and metadata."""

    model_config = ConfigDict(validate_assignment=True)

    redacted_text: str
    items: list[RedactionItem] = Field(default_factory=list)
    counts_by_type: dict[str, int] = Field(default_factory=dict)
    total_redactions: int = 0

    @property
    def has_redactions(self) -> bool:
        """True if any sensitive values were identified and masked."""
        return self.total_redactions > 0
