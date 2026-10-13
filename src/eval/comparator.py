"""Deterministic field-level comparison engine for parser evaluation."""
from __future__ import annotations

from datetime import date as dt_date, datetime as dt_datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from src.eval.models import EvaluationConfig, FieldComparisonResult, FieldMatchStatus
from src.models.models import Statement


def to_decimal(val: Any) -> Decimal | None:
    """Attempt conservative conversion of value to Decimal without float rounding errors."""
    if val is None:
        return None
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int,)):
        return Decimal(val)
    if isinstance(val, float):
        # Convert via repr to preserve literal string representation
        return Decimal(str(val))
    if isinstance(val, str):
        cleaned = val.strip().replace(",", "")
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None
    return None


def to_date_str(val: Any) -> str | None:
    """Normalize a date, datetime, or date string to ISO YYYY-MM-DD."""
    if val is None:
        return None
    if isinstance(val, (dt_date, dt_datetime)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, str):
        cleaned = val.strip()
        # Direct YYYY-MM-DD
        if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
            return cleaned
        try:
            d = dt_date.fromisoformat(cleaned)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def extract_field_value(statement: Statement | None, field_name: str) -> tuple[bool, Any]:
    """Safely extract a field value from a Statement using dot notation.

    Returns:
        (found, value): found is True if field was resolved (even if None/0/False),
        or False if field path does not exist or statement is None.
    """
    if statement is None:
        return (False, None)

    # Synthetic count helpers
    if field_name == "holdings_count":
        return (True, len(statement.holdings))
    if field_name == "transactions_count":
        return (True, len(statement.transactions))

    # Dot-path navigation (e.g. 'summary.closing_value')
    if "." in field_name:
        parts = field_name.split(".")
        current: Any = statement
        for part in parts:
            if current is None:
                return (True, None)
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                else:
                    return (False, None)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return (False, None)
        return (True, current)

    # Top-level Statement attribute
    if hasattr(statement, field_name):
        return (True, getattr(statement, field_name))

    # Fallback: check inside statement.summary if present
    if statement.summary and hasattr(statement.summary, field_name):
        return (True, getattr(statement.summary, field_name))

    return (False, None)


def compare_field_value(
    field_name: str,
    expected: Any,
    actual: Any,
    is_extracted: bool = True,
    config: EvaluationConfig | None = None,
) -> FieldComparisonResult:
    """Compare an actual extracted field value against expected ground truth.

    Args:
        field_name: Identifier for the field.
        expected: Expected ground truth value.
        actual: Extracted value from statement.
        is_extracted: True if field was resolved in statement model.
        config: Evaluation configuration settings (tolerances, formats).

    Returns:
        FieldComparisonResult with match status and discrepancy explanation.
    """
    cfg = config or EvaluationConfig()

    # Case 1: Expected is None (caller explicitly expected None or omitted field)
    if expected is None:
        if actual is None or not is_extracted:
            return FieldComparisonResult(
                field_name=field_name,
                expected=None,
                actual=actual,
                status=FieldMatchStatus.CORRECT,
            )
        else:
            return FieldComparisonResult(
                field_name=field_name,
                expected=None,
                actual=actual,
                status=FieldMatchStatus.UNEXPECTED,
                difference=f"Expected None, but extracted actual value {actual!r}",
            )

    # Case 2: Expected has a value, but actual was not extracted or is None
    if not is_extracted or actual is None:
        return FieldComparisonResult(
            field_name=field_name,
            expected=expected,
            actual=None,
            status=FieldMatchStatus.MISSING,
            difference=f"Expected {expected!r}, but field was missing or None",
        )

    # Case 3: Both values are present -> Compare values with conservative normalizations

    # 3.1. Boolean comparison (must check before int/Decimal since bool is subclass of int)
    if isinstance(expected, bool) or isinstance(actual, bool):
        if expected is actual:
            return FieldComparisonResult(
                field_name=field_name,
                expected=expected,
                actual=actual,
                status=FieldMatchStatus.CORRECT,
            )
        else:
            return FieldComparisonResult(
                field_name=field_name,
                expected=expected,
                actual=actual,
                status=FieldMatchStatus.INCORRECT,
                difference=f"Expected boolean {expected}, got {actual}",
            )

    # 3.2. Decimal / Numeric comparison
    exp_dec = to_decimal(expected)
    act_dec = to_decimal(actual)
    if exp_dec is not None and act_dec is not None:
        diff = abs(act_dec - exp_dec)
        if diff <= cfg.monetary_tolerance:
            return FieldComparisonResult(
                field_name=field_name,
                expected=expected,
                actual=actual,
                status=FieldMatchStatus.CORRECT,
            )
        else:
            return FieldComparisonResult(
                field_name=field_name,
                expected=expected,
                actual=actual,
                status=FieldMatchStatus.INCORRECT,
                difference=f"Numeric discrepancy: expected {exp_dec}, got {act_dec} (diff: {diff} > tol: {cfg.monetary_tolerance})",
            )

    # 3.3. Date normalization comparison
    if cfg.date_format_normalization:
        exp_date = to_date_str(expected)
        act_date = to_date_str(actual)
        if exp_date is not None and act_date is not None:
            if exp_date == act_date:
                return FieldComparisonResult(
                    field_name=field_name,
                    expected=expected,
                    actual=actual,
                    status=FieldMatchStatus.CORRECT,
                )
            else:
                return FieldComparisonResult(
                    field_name=field_name,
                    expected=expected,
                    actual=actual,
                    status=FieldMatchStatus.INCORRECT,
                    difference=f"Date mismatch: expected {exp_date}, got {act_date}",
                )

    # 3.4. Enum unwrapping
    exp_val = expected.value if isinstance(expected, Enum) else expected
    act_val = actual.value if isinstance(actual, Enum) else actual

    # 3.5. String / Generic comparison
    exp_str = str(exp_val).strip()
    act_str = str(act_val).strip()
    if exp_str == act_str:
        return FieldComparisonResult(
            field_name=field_name,
            expected=expected,
            actual=actual,
            status=FieldMatchStatus.CORRECT,
        )

    return FieldComparisonResult(
        field_name=field_name,
        expected=expected,
        actual=actual,
        status=FieldMatchStatus.INCORRECT,
        difference=f"Value mismatch: expected {expected!r}, got {actual!r}",
    )
