"""Configurable tolerance thresholds for validation checks.

All checks that compare computed vs. reported values use these tolerances to
avoid false positives from PDF display rounding (e.g. values formatted to 2 d.p.).

Defaults follow the Project Plan v4 spec; they can be overridden per-spec via the
``tolerances`` block in the ParsingSpec JSON or by constructing a custom instance.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ValidatorConfig(BaseModel):
    """Tolerance thresholds applied by each validation check.

    - Monetary tolerances: absolute value in the statement's currency.
    - Percentage tolerances: percentage points (e.g. ``Decimal("0.5")`` means ±0.5%).
    """

    model_config = ConfigDict(validate_assignment=True)

    # --- Cash reconciliation ----------------------------------------------- #
    cash_tolerance: Decimal = Decimal("1.00")
    """Max absolute deviation allowed between computed and reported closing_cash.

    Default of 1.00 accommodates rounding of individual amounts displayed in the PDF
    (each amount rounded to 2 d.p. can accumulate ~0.005 per transaction; 1.00 covers
    up to ~200 transactions without generating a false positive).
    """

    # --- Portfolio weights -------------------------------------------------- #
    weights_tolerance_pct: Decimal = Decimal("0.50")
    """Max allowed absolute deviation of Σ(holding.weight) from 100%.

    Default 0.50% per Project Plan v4.  Applied as: abs(sum − 100) ≤ tolerance.
    """

    # --- NAV consistency (AIF) --------------------------------------------- #
    nav_tolerance_pct: Decimal = Decimal("1.00")
    """Max relative deviation between units × nav_per_unit and closing_value.

    Default 1.00% per Project Plan v4.  Applied as:
        abs(computed − reported) / reported ≤ tolerance / 100
    """

    # --- Quantity × price versus transaction/holding value ----------------- #
    check_quantity_price: bool = True
    """When False, the quantity × price vs transaction/holding value check is disabled."""

    price_tolerance: Decimal = Decimal("1.00")
    """Max absolute deviation allowed between quantity × price and reported value."""

    price_tolerance_pct: Decimal = Decimal("1.00")
    """Max relative deviation percentage allowed between quantity × price and value."""

    max_transaction_charge_pct: Decimal = Decimal("2.00")
    """Max transaction charges as percentage of gross trade value allowed before error."""

    # --- Transaction sign vs type ------------------------------------------ #
    # No numeric tolerance; sign check is binary.  Included for completeness.
    check_transaction_signs: bool = True
    """When False, the sign-vs-type check is completely disabled."""

    # --- ISIN checksum ----------------------------------------------------- #
    check_isin_checksums: bool = True
    """When False, the ISIN Luhn check is completely disabled."""

    # --- Transaction dates within period ------------------------------------ #
    check_transaction_dates: bool = True
    """When False, the date-in-period check is completely disabled."""
