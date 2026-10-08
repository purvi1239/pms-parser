"""ISIN generation with valid check digits.

ISINs follow the format: CC + 9 alphanumeric + 1 check digit
Indian ISINs: INE + 6 alphanumeric + 1 check digit
Check digit uses a modified Luhn algorithm on the numeric expansion.
"""
from __future__ import annotations


def _char_to_digits(ch: str) -> str:
    """Expand a character to its numeric value: 0-9 → '0'-'9', A=10, B=11, …"""
    if ch.isdigit():
        return ch
    return str(ord(ch.upper()) - 55)  # A→10, B→11, …, Z→35


def _luhn_check_digit(payload: str) -> int:
    """Compute the Luhn check digit for an ISIN payload (country + 9 chars)."""
    digits = "".join(_char_to_digits(c) for c in payload)
    total = 0
    double_flag = True  # rightmost triggers doubling first
    for d in reversed(digits):
        n = int(d)
        if double_flag:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        double_flag = not double_flag
    return (10 - (total % 10)) % 10


def make_isin(country: str, body: str) -> str:
    """Construct an ISIN with a valid check digit.

    Args:
        country: Two-letter country code, e.g. "IN".
        body: 9-character alphanumeric string (will be zero-padded if shorter).

    Returns:
        12-character ISIN string.
    """
    if len(country) != 2 or not country.isalpha():
        raise ValueError(f"Country code must be 2 letters, got {country!r}")
    body = body.upper().zfill(9)[:9]
    payload = country.upper() + body
    check = _luhn_check_digit(payload)
    return payload + str(check)


def validate_isin(isin: str) -> bool:
    """Return True if the ISIN has a valid check digit."""
    if len(isin) != 12:
        return False
    payload, check_char = isin[:-1], isin[-1]
    if not check_char.isdigit():
        return False
    return _luhn_check_digit(payload) == int(check_char)


# Pre-built synthetic ISINs for reuse across sample generators
SYNTHETIC_ISINS: dict[str, str] = {
    "RELIANCE":   make_isin("IN", "E002A01018"),
    "TCS":        make_isin("IN", "E467B01029"),
    "INFY":       make_isin("IN", "E009A01021"),
    "HDFC_BANK":  make_isin("IN", "E040A01026"),
    "ICICI_BANK": make_isin("IN", "E090A01021"),
    "WIPRO":      make_isin("IN", "E075A01022"),
    "LT":         make_isin("IN", "E018A01030"),
    "BAJAJ_FIN":  make_isin("IN", "E296A01024"),
    "MARUTI":     make_isin("IN", "E585B01010"),
    "AXIS_BANK":  make_isin("IN", "E238A01034"),
    "SUNPHARMA":  make_isin("IN", "E044A01036"),
    "KOTAKBANK":  make_isin("IN", "E090A01021"),
    # AIF units (notional)
    "AIF_UNIT_1": make_isin("IN", "F001X01001"),
    "AIF_UNIT_2": make_isin("IN", "F002X01002"),
}
