"""Core redactor for identifying and masking sensitive PII before LLM dispatch."""
from __future__ import annotations

import re
from typing import Sequence

from src.redactor.models import (
    EntityType,
    RedactionConfig,
    RedactionItem,
    RedactionResult,
)
from src.redactor.patterns import (
    AADHAAR_FORMATTED_PATTERN,
    AADHAAR_LABELED_PATTERN,
    BANK_ACCOUNT_PATTERN,
    DEMAT_LABELED_PATTERN,
    DEMAT_STANDALONE_NSDL_PATTERN,
    EMAIL_PATTERN,
    NAME_LABELED_PATTERN,
    PAN_PATTERN,
    PHONE_PATTERN,
    is_financial_number,
    is_isin,
)


_ENTITY_PRIORITY: dict[EntityType, int] = {
    EntityType.CUSTOM: 0,
    EntityType.DEMAT: 1,
    EntityType.PAN: 2,
    EntityType.AADHAAR: 3,
    EntityType.NAME: 4,
    EntityType.BANK_ACCOUNT: 5,
    EntityType.PHONE: 6,
    EntityType.EMAIL: 7,
}


class _RawSpan:
    """Internal helper representing a detected sensitive span before replacement."""

    __slots__ = ("start", "end", "raw_val", "entity_type")

    def __init__(self, start: int, end: int, raw_val: str, entity_type: EntityType) -> None:
        self.start = start
        self.end = end
        self.raw_val = raw_val
        self.entity_type = entity_type


class Redactor:
    """Identifies and masks sensitive PII identifiers in text.

    Guarantees:
    - Deterministic, non-probabilistic regex & anchor matching.
    - Repeated occurrences of the exact same sensitive value are masked consistently.
    - Original sensitive values are NEVER retained in metadata, logs, or results.
    - Preserves ISINs, financial values, transaction figures, and dates without corruption.
    """

    def __init__(self, config: RedactionConfig | None = None) -> None:
        self.config = config or RedactionConfig()

    def redact(
        self,
        text: str,
        custom_sensitive_values: Sequence[str] | None = None,
    ) -> RedactionResult:
        """Mask sensitive values in text and return sanitized text and metadata.

        Args:
            text: Input string to sanitize.
            custom_sensitive_values: Additional explicit values (e.g. investor names,
                account numbers) to mask in addition to those in config.

        Returns:
            RedactionResult: Sanitized text and structured audit metadata.
        """
        if not text or not text.strip():
            return RedactionResult(
                redacted_text=text or "",
                items=[],
                counts_by_type={},
                total_redactions=0,
            )

        # 1. Collect all sensitive spans
        spans: list[_RawSpan] = []

        # Custom sensitive values (highest priority)
        all_custom = list(self.config.custom_sensitive_values)
        if custom_sensitive_values:
            all_custom.extend(custom_sensitive_values)

        for custom_val in all_custom:
            val_clean = custom_val.strip()
            if not val_clean:
                continue
            # Search case-insensitively
            for match in re.finditer(re.escape(val_clean), text, re.IGNORECASE):
                spans.append(
                    _RawSpan(
                        match.start(),
                        match.end(),
                        match.group(0),
                        EntityType.CUSTOM,
                    )
                )

        # Email addresses
        if self.config.redact_email:
            for match in EMAIL_PATTERN.finditer(text):
                spans.append(
                    _RawSpan(
                        match.start(),
                        match.end(),
                        match.group(0),
                        EntityType.EMAIL,
                    )
                )

        # PAN numbers
        if self.config.redact_pan:
            for match in PAN_PATTERN.finditer(text):
                val = match.group(0)
                # Guard against ISIN collision
                surrounding = text[max(0, match.start() - 2) : min(len(text), match.end() + 2)]
                if not is_isin(surrounding):
                    spans.append(
                        _RawSpan(
                            match.start(),
                            match.end(),
                            val,
                            EntityType.PAN,
                        )
                    )

        # Labeled Investor / Client Names
        if self.config.redact_names:
            for match in NAME_LABELED_PATTERN.finditer(text):
                raw_val = match.group(1)
                val = raw_val.rstrip()
                if val and len(val) >= 2:
                    # Span targets only the name value, preserving following punctuation
                    spans.append(
                        _RawSpan(
                            match.start(1),
                            match.start(1) + len(val),
                            val,
                            EntityType.NAME,
                        )
                    )

        # Indian Phone Numbers
        if self.config.redact_phone:
            for match in PHONE_PATTERN.finditer(text):
                val = match.group(0).strip()
                if not is_financial_number(val):
                    spans.append(
                        _RawSpan(
                            match.start(),
                            match.end(),
                            val,
                            EntityType.PHONE,
                        )
                    )

        # Aadhaar Numbers
        if self.config.redact_aadhaar:
            # Formatted 4-4-4
            for match in AADHAAR_FORMATTED_PATTERN.finditer(text):
                val = match.group(0).strip()
                if not is_financial_number(val) and not is_isin(val):
                    spans.append(
                        _RawSpan(
                            match.start(),
                            match.end(),
                            val,
                            EntityType.AADHAAR,
                        )
                    )
            # Labeled unformatted 12-digit
            for match in AADHAAR_LABELED_PATTERN.finditer(text):
                val = match.group(1).strip()
                spans.append(
                    _RawSpan(
                        match.start(1),
                        match.end(1),
                        val,
                        EntityType.AADHAAR,
                    )
                )

        # Bank Account Numbers
        if self.config.redact_bank_account:
            for match in BANK_ACCOUNT_PATTERN.finditer(text):
                # Guard against demat account overlap (e.g. "Demat A/c", "Demat Account")
                prefix = text[max(0, match.start() - 15) : match.start()].lower()
                if "demat" in prefix:
                    continue
                val = match.group(1).strip()
                # Target only the account number digits
                spans.append(
                    _RawSpan(
                        match.start(1),
                        match.end(1),
                        val,
                        EntityType.BANK_ACCOUNT,
                    )
                )

        # Demat Account Identifiers
        if self.config.redact_demat:
            # Labeled Demat
            for match in DEMAT_LABELED_PATTERN.finditer(text):
                val = match.group(1).strip()
                spans.append(
                    _RawSpan(
                        match.start(1),
                        match.end(1),
                        val,
                        EntityType.DEMAT,
                    )
                )
            # Standalone NSDL 16-char format (IN + 14 digits)
            for match in DEMAT_STANDALONE_NSDL_PATTERN.finditer(text):
                val = match.group(0).strip()
                spans.append(
                    _RawSpan(
                        match.start(),
                        match.end(),
                        val,
                        EntityType.DEMAT,
                    )
                )

        # 2. De-duplicate and resolve overlapping spans
        # Longest match wins, earlier start resolves ties, entity priority breaks further ties
        spans.sort(
            key=lambda s: (
                s.start,
                -(s.end - s.start),
                _ENTITY_PRIORITY.get(s.entity_type, 99),
            )
        )
        non_overlapping: list[_RawSpan] = []
        last_end = -1

        for span in spans:
            if span.start >= last_end:
                non_overlapping.append(span)
                last_end = span.end

        # 3. Consistent token assignment across repeated occurrences
        # Mapping: (entity_type, normalized_value) -> token string
        value_token_map: dict[tuple[EntityType, str], str] = {}
        type_counters: dict[EntityType, int] = {}

        def get_token(entity_type: EntityType, raw_val: str) -> str:
            norm_key = (entity_type, raw_val.strip().lower())
            if norm_key in value_token_map:
                return value_token_map[norm_key]

            base_template = self.config.token_templates.get(
                entity_type, f"[REDACTED_{entity_type.value.upper()}]"
            )

            if self.config.use_indexed_tokens:
                count = type_counters.get(entity_type, 0) + 1
                type_counters[entity_type] = count
                # e.g. [REDACTED_EMAIL] -> [REDACTED_EMAIL_1]
                indexed_token = base_template.rstrip("]") + f"_{count}]"
                value_token_map[norm_key] = indexed_token
                return indexed_token

            value_token_map[norm_key] = base_template
            return base_template

        # 4. Construct the redacted text and metadata items
        pieces: list[str] = []
        items: list[RedactionItem] = []
        counts_by_type: dict[str, int] = {}
        current_idx = 0

        for span in non_overlapping:
            # Append non-sensitive text before this span
            pieces.append(text[current_idx : span.start])

            # Get consistent replacement token
            token = get_token(span.entity_type, span.raw_val)

            # Offsets in the redacted text
            token_start = sum(len(p) for p in pieces)
            pieces.append(token)
            token_end = token_start + len(token)

            # Create item without raw value
            item = RedactionItem.from_match(
                entity_type=span.entity_type,
                token=token,
                start_char=token_start,
                end_char=token_end,
                raw_val=span.raw_val,
            )
            items.append(item)

            # Update count
            t_key = span.entity_type.value
            counts_by_type[t_key] = counts_by_type.get(t_key, 0) + 1

            current_idx = span.end

        # Append remaining text
        pieces.append(text[current_idx:])
        redacted_text = "".join(pieces)

        return RedactionResult(
            redacted_text=redacted_text,
            items=items,
            counts_by_type=counts_by_type,
            total_redactions=len(items),
        )


def redact(
    text: str,
    config: RedactionConfig | None = None,
    custom_sensitive_values: Sequence[str] | None = None,
) -> RedactionResult:
    """Convenience function to redact text with default or custom configuration."""
    return Redactor(config).redact(text, custom_sensitive_values=custom_sensitive_values)
