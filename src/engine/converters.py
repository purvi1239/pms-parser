"""Type conversion and normalization utilities for extracted statement values."""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from src.models.enums import TransactionType


def clean_text(val: Any) -> str | None:
    """Strip whitespace and return None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def to_decimal(val: Any) -> Decimal | None:
    """Parse a monetary or quantitative string into a Decimal.

    Handles currency symbols (₹, INR, $, €), commas, percentages,
    and accounting parenthesized negative values e.g. (1,234.50).
    Raises ValueError on invalid non-numeric strings.
    """
    if val is None:
        return None
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))

    s = str(val).strip()
    if not s or s in ("-", "--", "---", "—", "–", "−", "N/A", "NA", "nil", "None", ""):
        return None

    is_negative = False
    if s.startswith("(") and s.endswith(")"):
        is_negative = True
        s = s[1:-1].strip()
    elif s.startswith(("-", "−", "–")):
        is_negative = True
        s = s[1:].strip()
    elif s.endswith(("-", "−", "–")):
        is_negative = True
        s = s[:-1].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    # Strip currency prefixes and suffixes
    s_stripped = re.sub(r"^(?:₹|INR|USD|EUR|GBP|\$|€|£)\s*", "", s, flags=re.IGNORECASE).strip()
    s_stripped = re.sub(r"\s*(?:%|INR|USD|EUR|GBP)$", "", s_stripped, flags=re.IGNORECASE).strip()

    # Check for placeholder dash after currency prefix, e.g. "₹ —" or "₹ -"
    if s_stripped in ("-", "--", "---", "—", "–", "−", "N/A", "NA", "nil", "None"):
        return None

    # Check again for minus sign or parens inside/after currency prefix, e.g. ₹-110,000.00 or ₹(110,000.00)
    if s_stripped.startswith("(") and s_stripped.endswith(")"):
        is_negative = True
        s_stripped = s_stripped[1:-1].strip()
    elif s_stripped.startswith(("-", "−", "–")):
        is_negative = True
        s_stripped = s_stripped[1:].strip()
    elif s_stripped.endswith(("-", "−", "–")):
        is_negative = True
        s_stripped = s_stripped[:-1].strip()
    elif s_stripped.startswith("+"):
        s_stripped = s_stripped[1:].strip()

    # Remove commas in numbers
    s_clean = s_stripped.replace(",", "").strip()

    # Verify that the remaining string is a valid positive decimal number
    if not re.match(r"^\d+(?:\.\d+)?$", s_clean):
        raise ValueError(f"Cannot convert '{val}' to Decimal: invalid numeric characters")

    try:
        dec = Decimal(s_clean)
        if is_negative and dec != Decimal(0):
            return -dec
        return dec
    except (InvalidOperation, ValueError) as err:
        raise ValueError(f"Cannot convert '{val}' to Decimal: {err}") from err


_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
]


def to_date(val: Any) -> datetime.date | str | None:
    """Parse string to datetime.date or return cleaned string if unparseable."""
    if val is None:
        return None
    if isinstance(val, datetime.date):
        return val
    if isinstance(val, datetime.datetime):
        return val.date()

    s = str(val).strip()
    if not s or s in ("-", "--", "---", "—", "–", "−", "N/A", "NA", "nil", "None", ""):
        return None

    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    return s


def to_transaction_type(val: Any) -> TransactionType | None:
    """Convert an activity or transaction type string into TransactionType enum."""
    if val is None:
        return None
    if isinstance(val, TransactionType):
        return val

    s = str(val).strip().lower()
    alias_map: dict[str, TransactionType] = {
        "buy": TransactionType.BUY,
        "purchase": TransactionType.BUY,
        "subscription": TransactionType.BUY,
        "allotment": TransactionType.BUY,
        "sell": TransactionType.SELL,
        "sale": TransactionType.SELL,
        "redemption": TransactionType.SELL,
        "div": TransactionType.DIVIDEND,
        "dividend": TransactionType.DIVIDEND,
        "div payout": TransactionType.DIVIDEND,
        "dividend received": TransactionType.DIVIDEND,
        "fee": TransactionType.FEE,
        "fees": TransactionType.FEE,
        "charge": TransactionType.FEE,
        "charges": TransactionType.FEE,
        "mgmt fee": TransactionType.FEE,
        "management fee": TransactionType.FEE,
        "inflow": TransactionType.INFLOW,
        "funds added": TransactionType.INFLOW,
        "deposit": TransactionType.INFLOW,
        "outflow": TransactionType.OUTFLOW,
        "withdrawal": TransactionType.OUTFLOW,
        "funds withdrawn": TransactionType.OUTFLOW,
    }
    if s in alias_map:
        return alias_map[s]

    try:
        return TransactionType(s)
    except (ValueError, KeyError):
        return None
