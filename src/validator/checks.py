"""Individual validation check functions for parsed PMS/AIF statements.

Design rules
------------
- Each check returns ``(findings, was_run: bool)``.
- ``was_run=False`` means the check was **skipped** because required data was absent.
  The caller records this as a skipped check rather than a finding.
- Checks never raise exceptions; they record a WARNING finding and return gracefully
  when inputs are ambiguous.
- No extracted financial values are modified by any check.
- All Decimal arithmetic uses explicit quantization where comparisons are made.

Sign convention (Project Plan v4)
-----------------------------------
Transaction.amount is the **signed cash impact on the portfolio**:
    + : cash enters portfolio  (INFLOW, SELL, DIVIDEND)
    − : cash leaves portfolio  (BUY, FEE, OUTFLOW)
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import TYPE_CHECKING

from src.models.enums import StatementType, TransactionType
from src.models.models import Holding, ParseResult, Summary, Transaction
from src.validator.config import ValidatorConfig
from src.validator.models import ValidationFinding, ValidationSeverity

if TYPE_CHECKING:
    pass  # avoid circular imports if needed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _q2(val: Decimal) -> Decimal:
    return val.quantize(_Q2, rounding=ROUND_HALF_UP)


def _err(check: str, message: str, **kwargs) -> ValidationFinding:
    return ValidationFinding(
        check=check, severity=ValidationSeverity.ERROR, message=message, **kwargs
    )


def _warn(check: str, message: str, **kwargs) -> ValidationFinding:
    return ValidationFinding(
        check=check, severity=ValidationSeverity.WARNING, message=message, **kwargs
    )


def _get_summary(result: ParseResult) -> Summary | None:
    return result.summary or (result.statement.summary if result.statement else None)


def _get_holdings(result: ParseResult) -> list[Holding]:
    return result.holdings or (result.statement.holdings if result.statement else [])


def _get_transactions(result: ParseResult) -> list[Transaction]:
    return result.transactions or (result.statement.transactions if result.statement else [])


# ---------------------------------------------------------------------------
# ISIN Luhn check digit validation
# (Implemented inline to avoid importing from the dev-only sample_generator)
# ---------------------------------------------------------------------------

def _char_to_digits(ch: str) -> str:
    """Expand an ISIN character to its numeric string representation."""
    if ch.isdigit():
        return ch
    return str(ord(ch.upper()) - 55)  # A→10, B→11, …, Z→35


def validate_isin_checksum(isin: str) -> bool:
    """Return True if the ISIN's check digit is valid (Luhn algorithm).

    Format: 2-char country code + 9 alphanumeric body chars + 1 numeric check digit.
    The check digit is computed over the numeric expansion of the 11-char payload.
    """
    if not isin or len(isin) != 12:
        return False
    payload, check_char = isin[:-1], isin[-1]
    if not check_char.isdigit():
        return False
    digits = "".join(_char_to_digits(c) for c in payload)
    total = 0
    double_flag = True  # rightmost digit position triggers doubling first
    for d in reversed(digits):
        n = int(d)
        if double_flag:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        double_flag = not double_flag
    computed_check = (10 - (total % 10)) % 10
    return computed_check == int(check_char)


# ---------------------------------------------------------------------------
# Period parsing
# ---------------------------------------------------------------------------

_PERIOD_PATTERNS = [
    # "01 January 2026 to 31 March 2026"
    (
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+to\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
        "%d %B %Y",
    ),
    # "January 01, 2026 to March 31, 2026"
    (
        r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\s+to\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})",
        "%B %d %Y",
    ),
    # "2026-01-01 to 2026-03-31"
    (
        r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        "%Y-%m-%d",
    ),
    # "01/01/2026 to 31/03/2026"
    (
        r"(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})",
        "%d/%m/%Y",
    ),
]


def _parse_statement_period(period_str: str | None) -> tuple[datetime.date | None, datetime.date | None]:
    """Attempt to extract (start_date, end_date) from a statement_period string.

    Returns (None, None) if parsing fails or input is None.
    """
    if not period_str:
        return None, None

    s = period_str.strip()

    # Try pattern: "DD Month YYYY to DD Month YYYY"
    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+to\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s
    )
    if m:
        try:
            start = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            ).date()
            end = datetime.datetime.strptime(
                f"{m.group(4)} {m.group(5)} {m.group(6)}", "%d %B %Y"
            ).date()
            return start, end
        except ValueError:
            pass

    # Try pattern: "Month DD, YYYY to Month DD, YYYY"
    m = re.search(
        r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\s+to\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s
    )
    if m:
        try:
            start = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y"
            ).date()
            end = datetime.datetime.strptime(
                f"{m.group(4)} {m.group(5)} {m.group(6)}", "%B %d %Y"
            ).date()
            return start, end
        except ValueError:
            pass

    # Try ISO: "YYYY-MM-DD to YYYY-MM-DD"
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            start = datetime.date.fromisoformat(m.group(1))
            end = datetime.date.fromisoformat(m.group(2))
            return start, end
        except ValueError:
            pass

    # Try DD/MM/YYYY
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})", s)
    if m:
        try:
            start = datetime.datetime.strptime(m.group(1), "%d/%m/%Y").date()
            end = datetime.datetime.strptime(m.group(2), "%d/%m/%Y").date()
            return start, end
        except ValueError:
            pass

    return None, None


# ---------------------------------------------------------------------------
# Check 1 — Cash reconciliation
# ---------------------------------------------------------------------------

_CASH_CHECK = "cash_reconciliation"


def check_cash_reconciliation(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify: closing_cash = opening_cash + Σ(transaction.amount).

    Skipped when opening_cash, closing_cash, or transaction list is absent.
    Records a WARNING when any transaction has a None amount (degraded check).
    Returns ERROR when the absolute deviation exceeds config.cash_tolerance.
    """
    summary = _get_summary(result)
    if summary is None:
        return [], False

    opening = summary.opening_cash
    closing = summary.closing_cash

    if opening is None or closing is None:
        return [], False  # Required fields absent → skip

    transactions = _get_transactions(result)
    if not transactions:
        return [], False  # Nothing to reconcile

    findings: list[ValidationFinding] = []
    null_amount_count = 0
    txn_sum = _ZERO

    for t in transactions:
        if t.amount is None:
            null_amount_count += 1
        else:
            txn_sum += t.amount

    if null_amount_count > 0:
        findings.append(
            _warn(
                _CASH_CHECK,
                f"{null_amount_count} transaction(s) have no amount; cash reconciliation "
                f"may be incomplete.",
                field="transactions.amount",
            )
        )

    computed = _q2(opening + txn_sum)
    reported = _q2(closing)
    deviation = abs(computed - reported)

    if deviation > config.cash_tolerance:
        findings.append(
            _err(
                _CASH_CHECK,
                f"Cash reconciliation failed: opening_cash ({opening}) + Σ(amounts) "
                f"({txn_sum}) = {computed}, but closing_cash reported as {reported}. "
                f"Deviation {deviation} exceeds tolerance {config.cash_tolerance}.",
                field="summary.closing_cash",
                actual=str(reported),
                expected=str(computed),
            )
        )

    return findings, True


# ---------------------------------------------------------------------------
# Check 2 — NAV consistency (AIF only)
# ---------------------------------------------------------------------------

_NAV_CHECK = "nav_consistency"


def check_nav_consistency(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify: units × nav_per_unit ≈ closing_value (AIF only).

    Skipped for PMS statements or when any of the three values is absent.
    Returns ERROR when the relative deviation exceeds config.nav_tolerance_pct.
    """
    stmt = result.statement
    if stmt is None or stmt.statement_type != StatementType.AIF:
        return [], False

    summary = _get_summary(result)
    if summary is None:
        return [], False

    units = summary.units
    nav = summary.nav_per_unit
    closing = summary.closing_value

    if units is None or nav is None or closing is None:
        # Missing one or more AIF fields → warn rather than silently skip
        missing = [n for n, v in [("units", units), ("nav_per_unit", nav), ("closing_value", closing)] if v is None]
        return [
            _warn(
                _NAV_CHECK,
                f"NAV consistency check skipped: {', '.join(missing)} not available in summary.",
                field="summary",
            )
        ], False

    if closing == _ZERO:
        return [], False  # Avoid division by zero

    computed = _q2(units * nav)
    deviation_abs = abs(computed - _q2(closing))
    deviation_pct = (deviation_abs / abs(closing)) * _HUNDRED

    if deviation_pct > config.nav_tolerance_pct:
        return [
            _err(
                _NAV_CHECK,
                f"NAV consistency failed: units ({units}) × nav_per_unit ({nav}) = "
                f"{computed}, but closing_value = {closing}. "
                f"Relative deviation {deviation_pct:.4f}% exceeds tolerance {config.nav_tolerance_pct}%.",
                field="summary.closing_value",
                actual=str(_q2(closing)),
                expected=str(computed),
            )
        ], True

    return [], True


# ---------------------------------------------------------------------------
# Check 3 — Portfolio weights sum ≈ 100%
# ---------------------------------------------------------------------------

_WEIGHTS_CHECK = "portfolio_weights"


def check_portfolio_weights(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify Σ(holding.weight) ≈ 100% within config.weights_tolerance_pct.

    Skipped when no holding has a weight value.
    Records a WARNING (not ERROR) when some but not all holdings have weights,
    because partial weight data makes the sum inherently incomplete.
    """
    holdings = _get_holdings(result)
    if not holdings:
        return [], False

    weights = [h.weight for h in holdings if h.weight is not None]
    if not weights:
        return [], False  # No weight data at all → skip

    total = sum(weights, _ZERO)
    deviation = abs(total - _HUNDRED)

    missing_count = len(holdings) - len(weights)
    if missing_count > 0:
        return [
            _warn(
                _WEIGHTS_CHECK,
                f"Weight sum check degraded: {missing_count} of {len(holdings)} holdings "
                f"have no weight. Partial sum = {total:.4f}%.",
                field="holdings.weight",
                actual=str(total),
            )
        ], True

    if deviation > config.weights_tolerance_pct:
        return [
            _err(
                _WEIGHTS_CHECK,
                f"Portfolio weights do not sum to 100%: Σ(weight) = {total:.4f}%. "
                f"Deviation {deviation:.4f}% exceeds tolerance {config.weights_tolerance_pct}%.",
                field="holdings.weight",
                actual=f"{total:.4f}%",
                expected="100.00%",
            )
        ], True

    return [], True


# ---------------------------------------------------------------------------
# Check 4 — Quantity × price versus transaction/holding value
# ---------------------------------------------------------------------------

_QTY_PRICE_CHECK = "quantity_price_value"


def check_quantity_price_value(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify quantity × price consistency against transaction amount and holding market value.

    For transactions:
    - Skipped when disabled via config or no transaction has both quantity and price.
    - If quantity and price exist but amount is None → records WARNING (incomplete data).
    - BUY: gross = qty * price. Cash outflow (|amount|) expected to equal gross_value + charges.
      If outflow < gross - tolerance: underpayment ERROR.
      If implied charges (outflow - gross) exceed max_transaction_charge_pct: ERROR.
    - SELL: gross = qty * price. Cash inflow (amount) expected to equal gross_value - charges.
      If inflow > gross + tolerance: overpayment ERROR.
      If implied charges (gross - inflow) exceed max_transaction_charge_pct: ERROR.
    - Other types with quantity and price: |amount| must equal gross within tolerance.

    For holdings:
    - If holding has quantity, price, and market_value:
      qty * price must equal market_value within tolerance.
    """
    if not config.check_quantity_price:
        return [], False

    transactions = _get_transactions(result)
    holdings = _get_holdings(result)

    findings: list[ValidationFinding] = []
    checked = False

    # 1. Transaction checks
    for idx, t in enumerate(transactions):
        t_qty = t.quantity
        t_price = getattr(t, "price", None)
        if t_qty is None or t_price is None or t_qty == _ZERO or t_price == _ZERO:
            continue

        security_hint = f" ({t.security})" if t.security else ""

        if t.amount is None:
            findings.append(
                _warn(
                    _QTY_PRICE_CHECK,
                    f"Transaction #{idx + 1}{security_hint}: quantity ({t_qty}) and "
                    f"price ({t_price}) are present, but amount is None; "
                    f"transaction value cannot be validated.",
                    field=f"transactions[{idx}].amount",
                )
            )
            continue

        checked = True
        gross_val = _q2(abs(t_qty) * abs(t_price))
        reported_amt = t.amount
        reported_abs = _q2(abs(reported_amt))

        # Tolerances
        abs_tol = config.price_tolerance
        pct_tol = _q2((gross_val * config.price_tolerance_pct) / _HUNDRED)
        tol = max(abs_tol, pct_tol)
        max_charge = _q2((gross_val * config.max_transaction_charge_pct) / _HUNDRED)

        if t.type == TransactionType.BUY:
            # For BUY, amount should be negative (cash outflow = reported_abs)
            cash_outflow = reported_abs
            if cash_outflow < gross_val - tol:
                diff = gross_val - cash_outflow
                findings.append(
                    _err(
                        _QTY_PRICE_CHECK,
                        f"Transaction #{idx + 1}{security_hint}: BUY of {t_qty} @ {t_price} "
                        f"requires gross value {gross_val}, but cash outflow is only {cash_outflow} "
                        f"(underpayment by {diff}).",
                        field=f"transactions[{idx}].amount",
                        actual=str(reported_amt),
                        expected=f"-{gross_val}",
                    )
                )
            elif cash_outflow > gross_val + tol:
                implied_charges = cash_outflow - gross_val
                if implied_charges > max_charge:
                    findings.append(
                        _err(
                            _QTY_PRICE_CHECK,
                            f"Transaction #{idx + 1}{security_hint}: quantity ({t_qty}) × "
                            f"price ({t_price}) = {gross_val}, but cash outflow is {cash_outflow}. "
                            f"Implied charges of {implied_charges} exceed maximum allowed charges "
                            f"({max_charge:.2f}, {config.max_transaction_charge_pct}%).",
                            field=f"transactions[{idx}].amount",
                            actual=str(reported_amt),
                            expected=f"-{gross_val}",
                        )
                    )

        elif t.type == TransactionType.SELL:
            # For SELL, amount should be positive (cash inflow = reported_amt)
            cash_inflow = reported_amt
            if cash_inflow > gross_val + tol:
                diff = cash_inflow - gross_val
                findings.append(
                    _err(
                        _QTY_PRICE_CHECK,
                        f"Transaction #{idx + 1}{security_hint}: SELL of {t_qty} @ {t_price} "
                        f"generates gross value {gross_val}, but cash inflow is {cash_inflow} "
                        f"(overpayment by {diff}).",
                        field=f"transactions[{idx}].amount",
                        actual=str(reported_amt),
                        expected=str(gross_val),
                    )
                )
            elif cash_inflow < gross_val - tol:
                implied_charges = gross_val - cash_inflow
                if implied_charges > max_charge:
                    findings.append(
                        _err(
                            _QTY_PRICE_CHECK,
                            f"Transaction #{idx + 1}{security_hint}: quantity ({t_qty}) × "
                            f"price ({t_price}) = {gross_val}, but cash inflow is {cash_inflow}. "
                            f"Implied charges of {implied_charges} exceed maximum allowed charges "
                            f"({max_charge:.2f}, {config.max_transaction_charge_pct}%).",
                            field=f"transactions[{idx}].amount",
                            actual=str(reported_amt),
                            expected=str(gross_val),
                        )
                    )

        else:
            deviation = abs(reported_abs - gross_val)
            if deviation > tol:
                findings.append(
                    _err(
                        _QTY_PRICE_CHECK,
                        f"Transaction #{idx + 1}{security_hint}: quantity ({t_qty}) × "
                        f"price ({t_price}) = {gross_val}, but reported amount is {reported_amt} "
                        f"(deviation {deviation} exceeds tolerance {tol}).",
                        field=f"transactions[{idx}].amount",
                        actual=str(reported_amt),
                        expected=str(gross_val),
                    )
                )

    # 2. Holdings checks (if holding has quantity and price)
    for idx, h in enumerate(holdings):
        h_qty = h.quantity
        h_price = getattr(h, "price", None)
        h_mkt = h.market_value
        if h_qty is None or h_price is None or h_mkt is None or h_qty == _ZERO or h_price == _ZERO:
            continue

        checked = True
        computed_val = _q2(abs(h_qty) * abs(h_price))
        reported_mkt = _q2(h_mkt)
        abs_tol = config.price_tolerance
        pct_tol = _q2((computed_val * config.price_tolerance_pct) / _HUNDRED)
        tol = max(abs_tol, pct_tol)
        deviation = abs(computed_val - reported_mkt)

        if deviation > tol:
            sec_hint = f" '{h.security_name}'" if h.security_name else f" #{idx + 1}"
            findings.append(
                _err(
                    _QTY_PRICE_CHECK,
                    f"Holding{sec_hint}: quantity ({h_qty}) × price ({h_price}) = "
                    f"{computed_val}, but market_value is {reported_mkt} "
                    f"(deviation {deviation} exceeds tolerance {tol}).",
                    field=f"holdings[{idx}].market_value",
                    actual=str(reported_mkt),
                    expected=str(computed_val),
                )
            )

    return findings, checked


# ---------------------------------------------------------------------------
# Check 5 — Transaction sign vs. type
# ---------------------------------------------------------------------------

_SIGN_CHECK = "transaction_sign"

# Expected sign for each TransactionType per Project Plan v4
_EXPECTED_SIGN: dict[TransactionType, str] = {
    TransactionType.INFLOW: "positive",
    TransactionType.SELL: "positive",
    TransactionType.DIVIDEND: "positive",
    TransactionType.BUY: "negative",
    TransactionType.FEE: "negative",
    TransactionType.OUTFLOW: "negative",
}


def _sign_ok(amount: Decimal, expected: str) -> bool:
    """Return True when `amount`'s sign matches `expected` ('positive' or 'negative')."""
    if expected == "positive":
        return amount > _ZERO
    return amount < _ZERO  # negative


def check_transaction_signs(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify each transaction's amount sign matches its declared type.

    Records an ERROR for each violated transaction.
    Skipped when check is disabled via config or no transactions have both type and amount.
    """
    if not config.check_transaction_signs:
        return [], False

    transactions = _get_transactions(result)
    if not transactions:
        return [], False

    findings: list[ValidationFinding] = []
    checked = False

    for idx, t in enumerate(transactions):
        if t.amount is None or t.amount == _ZERO:
            continue  # Zero-amount or missing → skip this row
        expected = _EXPECTED_SIGN.get(t.type)
        if expected is None:
            continue  # Unknown type → skip
        checked = True
        if not _sign_ok(t.amount, expected):
            security_hint = f" ({t.security})" if t.security else ""
            findings.append(
                _err(
                    _SIGN_CHECK,
                    f"Transaction #{idx + 1}{security_hint}: type '{t.type.value}' expects a "
                    f"{expected} amount, but amount = {t.amount}.",
                    field=f"transactions[{idx}].amount",
                    actual=str(t.amount),
                    expected=f"{expected} value",
                )
            )

    return findings, checked


# ---------------------------------------------------------------------------
# Check 5 — ISIN checksum (Luhn)
# ---------------------------------------------------------------------------

_ISIN_CHECK = "isin_checksum"


def check_isin_checksums(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify the Luhn check digit of each holding's ISIN.

    Records an ERROR for each holding with an invalid ISIN.
    ISINs that are None or empty are skipped without warning (missing ISIN is
    handled separately by the identity fields check).
    """
    if not config.check_isin_checksums:
        return [], False

    holdings = _get_holdings(result)
    if not holdings:
        return [], False

    findings: list[ValidationFinding] = []
    checked = False

    for idx, h in enumerate(holdings):
        isin = h.isin
        if not isin:
            continue
        checked = True
        # Basic format check first
        if len(isin) != 12 or not isin[-1].isdigit():
            findings.append(
                _err(
                    _ISIN_CHECK,
                    f"Holding '{h.security_name or idx}': ISIN '{isin}' has invalid format "
                    f"(expected 12 characters ending with a digit).",
                    field=f"holdings[{idx}].isin",
                    actual=isin,
                )
            )
            continue

        if not validate_isin_checksum(isin):
            findings.append(
                _err(
                    _ISIN_CHECK,
                    f"Holding '{h.security_name or idx}': ISIN '{isin}' has an invalid "
                    f"check digit (Luhn verification failed).",
                    field=f"holdings[{idx}].isin",
                    actual=isin,
                )
            )

    return findings, checked


# ---------------------------------------------------------------------------
# Check 7 — Transaction dates within statement period
# ---------------------------------------------------------------------------

_DATES_CHECK = "transaction_dates"


def check_transaction_dates(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Verify each transaction date falls within the declared statement period.

    Skipped when check is disabled, period cannot be parsed, or no transaction
    has a parseable date.
    Records WARNING (not ERROR) for out-of-period dates because statement period
    strings have many formats and parsing is best-effort.
    """
    if not config.check_transaction_dates:
        return [], False

    stmt = result.statement
    if stmt is None:
        return [], False

    period_start, period_end = _parse_statement_period(stmt.statement_period)
    if period_start is None or period_end is None:
        return [], False  # Cannot determine period → skip

    transactions = _get_transactions(result)
    if not transactions:
        return [], False

    findings: list[ValidationFinding] = []
    checked = False

    for idx, t in enumerate(transactions):
        d = t.date
        if d is None or not isinstance(d, datetime.date):
            continue  # String date or None → skip (can't compare reliably)
        checked = True
        if d < period_start or d > period_end:
            security_hint = f" ({t.security})" if t.security else ""
            findings.append(
                _warn(
                    _DATES_CHECK,
                    f"Transaction #{idx + 1}{security_hint} date {d} is outside statement "
                    f"period {period_start} to {period_end}.",
                    field=f"transactions[{idx}].date",
                    actual=str(d),
                    expected=f"{period_start} to {period_end}",
                )
            )

    return findings, checked


# ---------------------------------------------------------------------------
# Check 8 — Identity fields / structural completeness
# ---------------------------------------------------------------------------

_IDENTITY_CHECK = "identity_fields"


def check_identity_fields(
    result: ParseResult, config: ValidatorConfig
) -> tuple[list[ValidationFinding], bool]:
    """Check for missing key identity fields and structural completeness.

    These are informational warnings rather than errors because missing fields
    are often expected for certain statement types (e.g. no holdings in AIF).

    Checks performed:
    - PMS with zero holdings → WARNING
    - AIF with no units and no nav_per_unit → WARNING
    - Statement with no client_identifier → WARNING
    - Statement with no provider_name → WARNING
    - Summary present but all monetary fields None → WARNING
    """
    stmt = result.statement
    holdings = _get_holdings(result)
    summary = _get_summary(result)
    findings: list[ValidationFinding] = []

    if stmt is None:
        return [
            _warn(
                _IDENTITY_CHECK,
                "No Statement object found in ParseResult; identity checks skipped.",
                field="statement",
            )
        ], True

    # PMS zero holdings
    if stmt.statement_type == StatementType.PMS and not holdings:
        findings.append(
            _warn(
                _IDENTITY_CHECK,
                "PMS statement has zero holdings; this may indicate a parsing failure.",
                field="holdings",
            )
        )

    # AIF missing NAV fields
    if stmt.statement_type == StatementType.AIF:
        if summary is None or (summary.units is None and summary.nav_per_unit is None):
            findings.append(
                _warn(
                    _IDENTITY_CHECK,
                    "AIF statement is missing both 'units' and 'nav_per_unit'; "
                    "NAV identity cannot be verified.",
                    field="summary.units",
                )
            )

    # Missing client identifier
    if not stmt.client_identifier:
        findings.append(
            _warn(
                _IDENTITY_CHECK,
                "Statement is missing 'client_identifier'; cannot confirm document belongs "
                "to the expected client.",
                field="statement.client_identifier",
            )
        )

    # Missing provider name
    if not stmt.provider_name:
        findings.append(
            _warn(
                _IDENTITY_CHECK,
                "Statement is missing 'provider_name'.",
                field="statement.provider_name",
            )
        )

    # Summary all-None monetary fields
    if summary is not None:
        monetary_fields = [
            summary.opening_value, summary.closing_value,
            summary.opening_cash, summary.closing_cash,
        ]
        if all(v is None for v in monetary_fields):
            findings.append(
                _warn(
                    _IDENTITY_CHECK,
                    "Summary object is present but all primary monetary fields "
                    "(opening_value, closing_value, opening_cash, closing_cash) are None; "
                    "no identity checks can be performed.",
                    field="summary",
                )
            )

    return findings, True


# ---------------------------------------------------------------------------
# Public registry: ordered list of all check callables
# ---------------------------------------------------------------------------

ALL_CHECKS = [
    check_identity_fields,
    check_cash_reconciliation,
    check_nav_consistency,
    check_portfolio_weights,
    check_quantity_price_value,
    check_transaction_signs,
    check_isin_checksums,
    check_transaction_dates,
]
