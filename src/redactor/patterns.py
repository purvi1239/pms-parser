"""Regex patterns and detection helpers for sensitive PII identifiers.

Designed specifically for financial statements to prevent false-positive matches
against ISINs, transaction amounts, dates, percentages, and table headers.
"""
from __future__ import annotations

import re
from typing import Match

# ---------------------------------------------------------------------------
# Common Patterns
# ---------------------------------------------------------------------------

# Standard email address
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

# Indian Permanent Account Number (PAN): 5 letters + 4 digits + 1 letter
PAN_PATTERN = re.compile(
    r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"
)

# Indian phone numbers: Mobile (10 digits starting with 6-9) and landlines
# Handles optional +91, 0 prefix, and spacing/hyphens.
# Negative lookahead/behind ensures no currency symbols or decimals.
PHONE_PATTERN = re.compile(
    r"(?<![₹$€\w.,])(?:\+91[\s-]?)?(?:0)?[6-9]\d{4}[\s-]?\d{5}(?!\.\d|\d)"
    r"|(?<![₹$€\w.,])(?:\+91[\s-]?)?(?:0)?[6-9]\d{9}(?!\.\d|\d)"
    r"|(?<![₹$€\w.,])(?:\+91[\s-]?)?0\d{2,4}[\s-]?\d{6,8}(?!\.\d|\d)"
)

# Indian Aadhaar number: 12 digits (first digit 2-9)
# 1. Formatted 4-4-4: e.g. 5432 1234 5678 or 5432-1234-5678
AADHAAR_FORMATTED_PATTERN = re.compile(
    r"(?<![₹$€\d.,])\b[2-9]\d{3}[\s-]\d{4}[\s-]\d{4}\b(?!\.\d)"
)

# 2. Unformatted 12 digits, only when anchored near Aadhaar/UID keywords
# to prevent false-positive collisions with 12-digit transaction numbers or amounts
AADHAAR_LABELED_PATTERN = re.compile(
    r"(?i)\b(?:aadhaar|aadhar|uid(?:ai)?|unique\s*id(?:entifier)?)\s*[:#-]?\s*([2-9]\d{11})\b"
)

# Bank account numbers: 9 to 18 digits (or masked ****1234)
# Must be preceded by account-related labels to avoid clashing with monetary quantities.
# Excludes 'demat a/c' so that demat accounts are not misclassified as bank accounts.
BANK_ACCOUNT_PATTERN = re.compile(
    r"(?i)(?<!demat\s)\b(?:account\s*(?:no\.?|num(?:ber)?|#)?|a/c\s*(?:no\.?|#)?|bank\s*a/c|s/b\s*a/c|c/a\s*a/c|savings\s*a/c|current\s*a/c)\s*[:#-]?\s*([0-9]{9,18}|[xX*]{4,14}[0-9]{3,4})\b"
)

# Demat account identifiers:
# 1. Labeled: CDSL (16 digits), NSDL (IN + 14 digits), or 8+8 DP ID + Client ID
DEMAT_LABELED_PATTERN = re.compile(
    r"(?i)\b(?:demat\s*(?:a/c|account|no\.?|#)?|dp\s*id|client\s*id|bo\s*id|boid|beneficiary\s*(?:id|account|no\.?))\s*[:#-]?\s*(IN[0-9]{14}|[0-9]{16}|[0-9]{8}[\s-][0-9]{8}|[xX*]{6,12}[0-9]{4})\b"
)

# 2. Standalone NSDL Demat account (IN followed by exactly 14 digits = 16 chars)
# Note: ISINs are 12 characters (IN + 9 alphanumeric + 1 check digit), so length 16 is distinct
DEMAT_STANDALONE_NSDL_PATTERN = re.compile(
    r"\bIN[0-9]{14}\b"
)

# Contextual investor name pattern:
# Must be anchored to "Investor Name:", "Client Name:", etc.
# Commas are treated as delimiters rather than name characters to preserve following text.
NAME_LABELED_PATTERN = re.compile(
    r"(?i)\b(?:investor\s*name|client\s*name|name\s*of\s*(?:the\s*)?(?:investor|client|account\s*holder)|account\s*holder(?:\s*name)?)\s*[:#-]?\s*([A-Z][A-Za-z\s.\'-]{1,40})(?=\s*(?:\n|\r|,|;|\t|\||PAN|A/c|Account|Date|Address|\s{2,}|$))"
)


# ---------------------------------------------------------------------------
# Guard Functions (False Positive Filtering)
# ---------------------------------------------------------------------------

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")


def is_isin(text: str) -> bool:
    """Return True if text matches the 12-character ISIN security identifier format."""
    return bool(_ISIN_RE.fullmatch(text.strip()))


def is_financial_number(text: str) -> bool:
    """Return True if the text represents a currency amount, percentage, or date."""
    clean = text.strip()
    # Currency symbols or accounting parentheses
    if any(sym in clean for sym in ("₹", "INR", "Rs.", "$", "€", "%", "(", ")")):
        return True
    # Has decimal point with 2 or 4 digits (e.g. 1250000.75, 0.005)
    if re.search(r"\.\d{1,4}$", clean):
        return True
    # Contains Indian comma formatting (e.g. 10,00,000)
    if "," in clean and re.match(r"^\d{1,3}(,\d{2,3})*(\.\d+)?$", clean):
        return True
    return False
