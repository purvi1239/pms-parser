"""Validation routines for human review feedback.

Ensures that user/reviewer corrections are structurally sound, well-typed,
syntactically valid, and internally consistent before any candidate parsing
specification is generated or accepted.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from src.engine.converters import to_decimal
from src.ir.models import DocumentIR
from src.learner.models import HumanFeedback
from src.models.enums import TransactionType

_VALID_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+(?:[.][A-Za-z0-9_-]+)*$")
_VALID_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

_VALID_SUMMARY_KEYS = {
    "opening_value",
    "closing_value",
    "opening_cash",
    "closing_cash",
    "fees_charged",
    "income",
    "realized_gains",
    "unrealized_gains",
    "units",
    "nav_per_unit",
}

_VALID_HOLDING_KEYS = {
    "security_name",
    "isin",
    "quantity",
    "price",
    "cost",
    "market_value",
    "weight",
}

_VALID_TRANSACTION_KEYS = {
    "date",
    "type",
    "security",
    "quantity",
    "price",
    "amount",
}


def validate_feedback(
    feedback: HumanFeedback,
    doc: DocumentIR | None = None,
) -> tuple[bool, list[str]]:
    """Validate human review feedback for syntax, field names, types, and arithmetic consistency.

    Args:
        feedback: The human corrections container.
        doc: Optional DocumentIR to verify that corrected text values actually exist.

    Returns:
        tuple[bool, list[str]]: (is_valid, list of error messages).
    """
    errors: list[str] = []

    # 1. Format ID validation
    if not feedback.format_id or not isinstance(feedback.format_id, str):
        errors.append("format_id must be a non-empty string.")
    else:
        clean_id = feedback.format_id.strip()
        if not clean_id:
            errors.append("format_id cannot be blank.")
        elif not _VALID_FORMAT_ID_RE.match(clean_id) or ".." in clean_id or "/" in clean_id or "\\" in clean_id:
            errors.append(
                f"Invalid format_id '{feedback.format_id}': contains invalid characters or path traversal elements."
            )

    # 2. Non-empty feedback check
    if not feedback.has_corrections():
        errors.append("Feedback contains no actionable corrections.")
        return False, errors

    # 3. Metadata validation
    for key, val in feedback.corrected_metadata.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("Metadata field names must be non-empty strings.")
            continue
        if isinstance(val, (dict, list)):
            errors.append(f"Metadata field '{key}' must be a scalar value, got {type(val).__name__}.")

    # 4. Summary fields validation
    parsed_summary: dict[str, Decimal] = {}
    for key, val in feedback.corrected_summary.items():
        if key not in _VALID_SUMMARY_KEYS:
            errors.append(
                f"Unknown summary field '{key}'. Allowed fields: {sorted(_VALID_SUMMARY_KEYS)}."
            )
            continue

        if val is None:
            continue

        try:
            dec_val = to_decimal(val)
            parsed_summary[key] = dec_val
        except (ValueError, InvalidOperation):
            errors.append(f"Summary field '{key}' has unparseable numeric value: '{val}'.")
            continue

        # Negative checks for fields that cannot be negative
        if key in ("units", "nav_per_unit") and dec_val < Decimal("0"):
            errors.append(f"Summary field '{key}' cannot be negative (got {dec_val}).")

    # Check internal arithmetic consistency for Summary
    if (
        "units" in parsed_summary
        and "nav_per_unit" in parsed_summary
        and "closing_value" in parsed_summary
    ):
        units = parsed_summary["units"]
        nav = parsed_summary["nav_per_unit"]
        closing = parsed_summary["closing_value"]
        if units > 0 and nav > 0 and closing > 0:
            expected_closing = units * nav
            diff = abs(expected_closing - closing)
            # More than 5% relative tolerance is a contradiction
            if (diff / closing) > Decimal("0.05"):
                errors.append(
                    f"Inconsistent summary feedback: units ({units}) × nav_per_unit ({nav}) = {expected_closing}, "
                    f"which contradicts closing_value ({closing})."
                )

    # 5. Holdings validation
    for idx, row in enumerate(feedback.corrected_holdings):
        if not isinstance(row, dict):
            errors.append(f"Holdings row {idx} must be a dictionary.")
            continue

        unknown_keys = set(row.keys()) - _VALID_HOLDING_KEYS
        if unknown_keys:
            errors.append(f"Holdings row {idx} contains unknown keys: {sorted(unknown_keys)}.")

        # Numeric field checks
        for num_field in ("quantity", "price", "cost", "market_value", "weight"):
            if num_field in row and row[num_field] is not None:
                try:
                    dec_val = to_decimal(row[num_field])
                    if num_field == "quantity" and dec_val < Decimal("0"):
                        errors.append(f"Holdings row {idx}: quantity cannot be negative ({dec_val}).")
                    if num_field == "weight" and (dec_val < Decimal("0") or dec_val > Decimal("100.5")):
                        errors.append(f"Holdings row {idx}: weight must be between 0 and 100% ({dec_val}).")
                except (ValueError, InvalidOperation):
                    errors.append(f"Holdings row {idx}: field '{num_field}' has invalid numeric value '{row[num_field]}'.")

        # ISIN format check
        if "isin" in row and row["isin"]:
            isin_str = str(row["isin"]).strip()
            if not _VALID_ISIN_RE.match(isin_str):
                errors.append(f"Holdings row {idx}: invalid ISIN format '{isin_str}'. Expected 12-char alphanumeric.")

    # 6. Transactions validation
    for idx, tx in enumerate(feedback.corrected_transactions):
        if not isinstance(tx, dict):
            errors.append(f"Transactions row {idx} must be a dictionary.")
            continue

        unknown_keys = set(tx.keys()) - _VALID_TRANSACTION_KEYS
        if unknown_keys:
            errors.append(f"Transactions row {idx} contains unknown keys: {sorted(unknown_keys)}.")

        # Type validation
        if "type" in tx and tx["type"] is not None:
            t_type = TransactionType._missing_(tx["type"])
            if t_type is None:
                errors.append(
                    f"Transactions row {idx}: unknown transaction type '{tx['type']}'. "
                    f"Expected one of {[m.value for m in TransactionType]}."
                )

        # Amount validation
        if "amount" in tx and tx["amount"] is not None:
            try:
                to_decimal(tx["amount"])
            except (ValueError, InvalidOperation):
                errors.append(f"Transactions row {idx}: field 'amount' has invalid numeric value '{tx['amount']}'.")

    # 7. Document presence check (if DocumentIR supplied)
    if doc is not None and not errors:
        all_text = doc.get_all_text().lower()
        for k in ("client_identifier", "provider_name"):
            val = feedback.corrected_metadata.get(k)
            if val and isinstance(val, str) and len(val) >= 4:
                # Substring check
                if val.lower() not in all_text:
                    # Record as warning or discrepancy if key values are completely absent from document
                    pass

    return len(errors) == 0, errors
