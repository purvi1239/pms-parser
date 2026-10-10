"""Tests for Step 8: Statement Validator.

All tests use in-memory ParseResult fixtures — no PDF I/O, no database,
no LLM calls.  Designed to be fast to collect and deterministic.

Test coverage:
  1.  Validator imports and contract
  2.  Cash reconciliation — valid, invalid, tolerances, null amounts
  3.  NAV consistency — valid AIF, invalid, PMS skips, missing fields
  4.  Portfolio weights — sums to 100, off by more than tolerance, partial weights
  5.  Transaction sign vs. type — each TransactionType, boundary amounts
  6.  ISIN checksum — valid ISINs, bad check digit, bad format, None ISINs
  7.  Transaction date within period — in-period, out-of-period, unparseable period
  8.  Identity fields — missing client_id, provider, PMS zero holdings, AIF no NAV
  9.  Full validator (Validator.validate) — clean PMS, clean AIF, multiple findings
  10. ValidatorConfig customisation — tighter cash tolerance, disable checks
  11. Convenience function validate_result
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.models.models import Holding, ParseResult, Statement, Summary, Transaction
from src.validator import (
    ValidationSeverity,
    Validator,
    ValidatorConfig,
    check_cash_reconciliation,
    check_identity_fields,
    check_isin_checksums,
    check_nav_consistency,
    check_portfolio_weights,
    check_quantity_price_value,
    check_transaction_dates,
    check_transaction_signs,
    validate_isin_checksum,
    validate_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

D = Decimal


def _make_pms_result(
    holdings: list[Holding] | None = None,
    transactions: list[Transaction] | None = None,
    summary: Summary | None = None,
    provider_name: str | None = "Pinnacle Wealth PMS",
    client_identifier: str | None = "PWM-CLIENT-0042",
    statement_period: str | None = "01 January 2026 to 31 March 2026",
) -> ParseResult:
    """Build a minimal valid PMS ParseResult for testing."""
    stmt = Statement(
        provider_name=provider_name,
        strategy_name="India Growth Strategy",
        client_identifier=client_identifier,
        statement_period=statement_period,
        currency="INR",
        statement_type=StatementType.PMS,
        holdings=holdings or [],
        transactions=transactions or [],
        summary=summary,
    )
    return ParseResult(
        statement=stmt,
        holdings=holdings or [],
        transactions=transactions or [],
        summary=summary,
        status=DocumentStatus.PARSED,
    )


def _make_aif_result(
    transactions: list[Transaction] | None = None,
    summary: Summary | None = None,
    client_identifier: str | None = "NV-AIF-2026-0011",
    statement_period: str | None = "01 January 2026 to 31 March 2026",
) -> ParseResult:
    """Build a minimal valid AIF ParseResult for testing."""
    stmt = Statement(
        provider_name="Nexus Ventures AIF",
        client_identifier=client_identifier,
        statement_period=statement_period,
        statement_type=StatementType.AIF,
        holdings=[],
        transactions=transactions or [],
        summary=summary,
    )
    return ParseResult(
        statement=stmt,
        holdings=[],
        transactions=transactions or [],
        summary=summary,
        status=DocumentStatus.PARSED,
    )


# Known-valid Indian ISINs (verified by Luhn algorithm)
VALID_ISIN_RELIANCE = "INE002A01018"
VALID_ISIN_TCS      = "INE467B01029"
VALID_ISIN_INFY     = "INE009A01021"


# ---------------------------------------------------------------------------
# 1. Validator contract
# ---------------------------------------------------------------------------

class TestValidatorContract:
    def test_validate_result_returns_validation_result(self):
        result = validate_result(ParseResult())
        assert isinstance(result.findings, list)
        assert isinstance(result.checks_run, list)
        assert isinstance(result.checks_skipped, list)
        assert result.final_status in DocumentStatus.__members__.values()

    def test_passed_true_when_no_errors(self):
        result = validate_result(ParseResult())
        # Minimal ParseResult may have warnings but no errors
        assert result.passed is True or result.passed is False  # type: narrowing
        # No ERROR → passed
        vr = validate_result(ParseResult(status=DocumentStatus.PARSED))
        assert vr.passed == (not any(f.severity == ValidationSeverity.ERROR for f in vr.findings))

    def test_errors_and_warnings_properties(self):
        """errors / warnings properties correctly filter findings."""
        r = _make_pms_result(
            holdings=[Holding(isin="INVALID_ISIN_BAD")],  # triggers ISIN error
        )
        vr = validate_result(r)
        for f in vr.errors:
            assert f.severity == ValidationSeverity.ERROR
        for f in vr.warnings:
            assert f.severity == ValidationSeverity.WARNING


# ---------------------------------------------------------------------------
# 2. Cash reconciliation
# ---------------------------------------------------------------------------

class TestCashReconciliation:
    _cfg = ValidatorConfig()

    def _txns(self, amounts: list[str]) -> list[Transaction]:
        return [
            Transaction(type=TransactionType.INFLOW, amount=Decimal(a))
            for a in amounts
        ]

    def test_valid_cash_reconciliation(self):
        """opening + Σ(txns) == closing → no findings, check runs."""
        summary = Summary(
            opening_cash=D("80000.00"),
            closing_cash=D("93500.00"),
        )
        # 80000 + 200000 - 110000 - 78000 + 48000 + 12000 - 8500 - 50000 = 93500
        txns = [
            Transaction(type=TransactionType.INFLOW,   amount=D("200000.00")),
            Transaction(type=TransactionType.BUY,      amount=D("-110000.00")),
            Transaction(type=TransactionType.BUY,      amount=D("-78000.00")),
            Transaction(type=TransactionType.SELL,     amount=D("48000.00")),
            Transaction(type=TransactionType.DIVIDEND, amount=D("12000.00")),
            Transaction(type=TransactionType.FEE,      amount=D("-8500.00")),
            Transaction(type=TransactionType.OUTFLOW,  amount=D("-50000.00")),
        ]
        result = _make_pms_result(transactions=txns, summary=summary)
        findings, was_run = check_cash_reconciliation(result, self._cfg)
        assert was_run is True
        assert not any(f.severity == ValidationSeverity.ERROR for f in findings)

    def test_cash_reconciliation_error_exceeds_tolerance(self):
        """Deviation > tolerance → ERROR finding."""
        summary = Summary(
            opening_cash=D("80000.00"),
            closing_cash=D("90000.00"),  # wrong: should be 93500
        )
        txns = [
            Transaction(type=TransactionType.INFLOW,   amount=D("200000.00")),
            Transaction(type=TransactionType.BUY,      amount=D("-110000.00")),
            Transaction(type=TransactionType.BUY,      amount=D("-78000.00")),
            Transaction(type=TransactionType.SELL,     amount=D("48000.00")),
            Transaction(type=TransactionType.DIVIDEND, amount=D("12000.00")),
            Transaction(type=TransactionType.FEE,      amount=D("-8500.00")),
            Transaction(type=TransactionType.OUTFLOW,  amount=D("-50000.00")),
        ]
        result = _make_pms_result(transactions=txns, summary=summary)
        findings, was_run = check_cash_reconciliation(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "cash_reconciliation" in errors[0].check

    def test_cash_reconciliation_within_tolerance(self):
        """Deviation ≤ tolerance → no error (rounding artifact)."""
        summary = Summary(
            opening_cash=D("80000.00"),
            closing_cash=D("93500.50"),  # deviation = 0.50 = tolerance
        )
        txns = [
            Transaction(type=TransactionType.INFLOW, amount=D("13500.00")),
        ]
        cfg = ValidatorConfig(cash_tolerance=D("0.50"))
        result = _make_pms_result(transactions=txns, summary=summary)
        findings, _ = check_cash_reconciliation(result, cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors  # exactly at boundary → no error

    def test_cash_reconciliation_just_over_tight_tolerance(self):
        """Deviation > tight tolerance → error even for small amount."""
        summary = Summary(
            opening_cash=D("80000.00"),
            closing_cash=D("80001.60"),  # deviation = 0.60 > 0.50
        )
        txns = [
            Transaction(type=TransactionType.INFLOW, amount=D("1.00")),
        ]
        cfg = ValidatorConfig(cash_tolerance=D("0.50"))
        result = _make_pms_result(transactions=txns, summary=summary)
        findings, _ = check_cash_reconciliation(result, cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1

    def test_cash_reconciliation_skipped_when_no_summary(self):
        result = _make_pms_result()  # no summary
        _, was_run = check_cash_reconciliation(result, self._cfg)
        assert was_run is False

    def test_cash_reconciliation_skipped_when_opening_cash_none(self):
        summary = Summary(closing_cash=D("50000.00"))  # opening_cash is None
        txns = [Transaction(type=TransactionType.INFLOW, amount=D("50000.00"))]
        result = _make_pms_result(transactions=txns, summary=summary)
        _, was_run = check_cash_reconciliation(result, self._cfg)
        assert was_run is False

    def test_cash_reconciliation_warns_on_null_amounts(self):
        """Transactions with None amount → warning (check still runs)."""
        summary = Summary(opening_cash=D("0.00"), closing_cash=D("0.00"))
        txns = [
            Transaction(type=TransactionType.INFLOW, amount=None),
        ]
        result = _make_pms_result(transactions=txns, summary=summary)
        findings, was_run = check_cash_reconciliation(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("null" in w.message.lower() or "no amount" in w.message.lower() for w in warns)


# ---------------------------------------------------------------------------
# 3. NAV consistency
# ---------------------------------------------------------------------------

class TestNAVConsistency:
    _cfg = ValidatorConfig()

    def test_valid_nav_consistency(self):
        """units × nav_per_unit == closing_value → no findings, check runs."""
        # 480 × 970 = 465600
        summary = Summary(
            units=D("480.00"),
            nav_per_unit=D("970.00"),
            closing_value=D("465600.00"),
        )
        result = _make_aif_result(summary=summary)
        findings, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_nav_inconsistency_exceeds_tolerance(self):
        """Relative deviation > 1% → ERROR."""
        # 480 × 970 = 465600, but closing_value = 461000 → diff 4600 / 465600 ≈ 0.99% > 1%? 
        # Let's use 450000: diff = 15600 / 465600 ≈ 3.35%
        summary = Summary(
            units=D("480.00"),
            nav_per_unit=D("970.00"),
            closing_value=D("450000.00"),
        )
        result = _make_aif_result(summary=summary)
        findings, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "nav_consistency" in errors[0].check

    def test_nav_within_tolerance(self):
        """Relative deviation ≤ 1% → no error."""
        # 480 × 970 = 465600, closing = 465600 × 1.009 ≈ 469754 (0.9% off)
        closing = D("465600.00") * D("1.009")
        summary = Summary(
            units=D("480.00"),
            nav_per_unit=D("970.00"),
            closing_value=closing.quantize(D("0.01")),
        )
        result = _make_aif_result(summary=summary)
        findings, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_nav_check_skipped_for_pms(self):
        """NAV check does not run on PMS statements."""
        summary = Summary(units=D("100"), nav_per_unit=D("100"), closing_value=D("9000"))
        result = _make_pms_result(summary=summary)
        _, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is False

    def test_nav_check_warns_when_fields_missing(self):
        """AIF without units/nav → WARNING, not silently skipped."""
        summary = Summary(closing_value=D("465600.00"))  # no units, no nav
        result = _make_aif_result(summary=summary)
        findings, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is False
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert len(warns) == 1

    def test_nav_check_skipped_when_no_summary(self):
        result = _make_aif_result()
        _, was_run = check_nav_consistency(result, self._cfg)
        assert was_run is False


# ---------------------------------------------------------------------------
# 4. Portfolio weights
# ---------------------------------------------------------------------------

class TestPortfolioWeights:
    _cfg = ValidatorConfig()

    def test_weights_sum_to_100(self):
        holdings = [
            Holding(isin=VALID_ISIN_RELIANCE, weight=D("27.1739")),
            Holding(isin=VALID_ISIN_TCS,      weight=D("22.6087")),
            Holding(isin=VALID_ISIN_INFY,     weight=D("23.1884")),
            Holding(isin=VALID_ISIN_RELIANCE, weight=D("14.7826")),
            Holding(isin=VALID_ISIN_TCS,      weight=D("12.2464")),
        ]
        # Sum = 100.0000
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_portfolio_weights(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_weights_sum_off_by_over_tolerance(self):
        """Weights sum to 105 → ERROR."""
        holdings = [
            Holding(weight=D("55.00")),
            Holding(weight=D("50.00")),
        ]
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_portfolio_weights(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1

    def test_weights_within_tolerance(self):
        """Sum = 100.30 with tolerance 0.50 → no error."""
        holdings = [
            Holding(weight=D("50.15")),
            Holding(weight=D("50.15")),
        ]
        result = _make_pms_result(holdings=holdings)
        cfg = ValidatorConfig(weights_tolerance_pct=D("0.50"))
        findings, _ = check_portfolio_weights(result, cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_weights_just_over_tolerance(self):
        """Sum = 100.51 with tolerance 0.50 → ERROR."""
        holdings = [
            Holding(weight=D("50.255")),
            Holding(weight=D("50.255")),
        ]
        result = _make_pms_result(holdings=holdings)
        cfg = ValidatorConfig(weights_tolerance_pct=D("0.50"))
        findings, _ = check_portfolio_weights(result, cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1

    def test_weights_skipped_when_no_holdings(self):
        result = _make_pms_result()
        _, was_run = check_portfolio_weights(result, self._cfg)
        assert was_run is False

    def test_partial_weights_warning(self):
        """Some holdings have no weight → WARNING, not error."""
        holdings = [
            Holding(weight=D("50.00")),
            Holding(weight=None),   # missing
        ]
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_portfolio_weights(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert warns  # got a warning
        assert not errors  # no error for partial data


# ---------------------------------------------------------------------------
# 5. Quantity × price versus transaction/holding value
# ---------------------------------------------------------------------------

class TestQuantityPriceValue:
    _cfg = ValidatorConfig()

    def test_buy_exact_match_passes(self):
        """BUY transaction: 10 shares @ 100.00 = 1000.00, outflow = -1000.00."""
        t = Transaction(
            type=TransactionType.BUY,
            security="TCS",
            quantity=D("10"),
            price=D("100.00"),
            amount=D("-1000.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        assert not [f for f in findings if f.severity == ValidationSeverity.ERROR]

    def test_buy_with_acceptable_charges_passes(self):
        """BUY transaction with charges: gross = 1000.00, outflow = 1015.00 (charges = 1.5% <= 2.0%)."""
        t = Transaction(
            type=TransactionType.BUY,
            security="TCS",
            quantity=D("10"),
            price=D("100.00"),
            amount=D("-1015.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        assert not [f for f in findings if f.severity == ValidationSeverity.ERROR]

    def test_buy_with_excessive_charges_fails(self):
        """BUY transaction with excessive charges: gross = 1000.00, outflow = 1050.00 (charges = 5.0% > 2.0%)."""
        t = Transaction(
            type=TransactionType.BUY,
            security="TCS",
            quantity=D("10"),
            price=D("100.00"),
            amount=D("-1050.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "exceed maximum allowed charges" in errors[0].message

    def test_buy_underpayment_fails(self):
        """BUY transaction where cash outflow < gross trade value (outflow 950 vs gross 1000)."""
        t = Transaction(
            type=TransactionType.BUY,
            security="TCS",
            quantity=D("10"),
            price=D("100.00"),
            amount=D("-950.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "underpayment" in errors[0].message

    def test_sell_exact_match_passes(self):
        """SELL transaction: 20 shares @ 50.00 = 1000.00, inflow = +1000.00."""
        t = Transaction(
            type=TransactionType.SELL,
            security="INFY",
            quantity=D("20"),
            price=D("50.00"),
            amount=D("1000.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        assert not [f for f in findings if f.severity == ValidationSeverity.ERROR]

    def test_sell_with_acceptable_charges_passes(self):
        """SELL transaction: gross = 1000.00, inflow = 985.00 (charges = 1.5% <= 2.0%)."""
        t = Transaction(
            type=TransactionType.SELL,
            security="INFY",
            quantity=D("20"),
            price=D("50.00"),
            amount=D("985.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        assert not [f for f in findings if f.severity == ValidationSeverity.ERROR]

    def test_sell_overpayment_fails(self):
        """SELL transaction where cash inflow > gross value (inflow 1050 vs gross 1000)."""
        t = Transaction(
            type=TransactionType.SELL,
            security="INFY",
            quantity=D("20"),
            price=D("50.00"),
            amount=D("1050.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "overpayment" in errors[0].message

    def test_sell_excessive_charges_fails(self):
        """SELL transaction with excessive deduction (inflow 920 vs gross 1000 -> 8% charges)."""
        t = Transaction(
            type=TransactionType.SELL,
            security="INFY",
            quantity=D("20"),
            price=D("50.00"),
            amount=D("920.00"),
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "exceed maximum allowed charges" in errors[0].message

    def test_missing_amount_yields_warning(self):
        """Transaction with quantity and price but None amount yields WARNING."""
        t = Transaction(
            type=TransactionType.BUY,
            security="RELIANCE",
            quantity=D("15"),
            price=D("2500.00"),
            amount=None,
        )
        res = _make_pms_result(transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(warns) == 1
        assert not errors

    def test_holding_qty_price_match_passes(self):
        """Holding: 100 shares @ 250.00 = 25000.00, market_value = 25000.00."""
        h = Holding(
            security_name="Test Stock",
            quantity=D("100"),
            price=D("250.00"),
            market_value=D("25000.00"),
        )
        res = _make_pms_result(holdings=[h])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        assert not [f for f in findings if f.severity == ValidationSeverity.ERROR]

    def test_holding_qty_price_mismatch_fails(self):
        """Holding: 100 shares @ 250.00 = 25000.00, but market_value = 28000.00."""
        h = Holding(
            security_name="Mismatch Stock",
            quantity=D("100"),
            price=D("250.00"),
            market_value=D("28000.00"),
        )
        res = _make_pms_result(holdings=[h])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "market_value" in errors[0].field

    def test_skipped_when_no_price(self):
        """Standard transactions/holdings without price attributes skip check cleanly."""
        t = Transaction(type=TransactionType.BUY, quantity=D("10"), amount=D("-1000.00"))
        h = Holding(security_name="Normal Stock", quantity=D("50"), market_value=D("5000.00"))
        res = _make_pms_result(holdings=[h], transactions=[t])
        findings, was_run = check_quantity_price_value(res, self._cfg)
        assert was_run is False
        assert findings == []

    def test_disabled_by_config(self):
        t = Transaction(
            type=TransactionType.BUY,
            quantity=D("10"),
            price=D("100.00"),
            amount=D("-500.00"),  # mismatch
        )
        res = _make_pms_result(transactions=[t])
        cfg = ValidatorConfig(check_quantity_price=False)
        findings, was_run = check_quantity_price_value(res, cfg)
        assert was_run is False
        assert findings == []


# ---------------------------------------------------------------------------
# 6. Transaction sign vs. type
# ---------------------------------------------------------------------------

class TestTransactionSigns:
    _cfg = ValidatorConfig()

    @pytest.mark.parametrize("txn_type, amount, should_error", [
        (TransactionType.INFLOW,   D("200000.00"),  False),
        (TransactionType.SELL,     D("48000.00"),   False),
        (TransactionType.DIVIDEND, D("12000.00"),   False),
        (TransactionType.BUY,      D("-110000.00"), False),
        (TransactionType.FEE,      D("-8500.00"),   False),
        (TransactionType.OUTFLOW,  D("-50000.00"),  False),
        # Wrong signs
        (TransactionType.BUY,      D("50000.00"),   True),   # BUY must be negative
        (TransactionType.INFLOW,   D("-10000.00"),  True),   # INFLOW must be positive
        (TransactionType.SELL,     D("-5000.00"),   True),   # SELL must be positive
        (TransactionType.FEE,      D("500.00"),     True),   # FEE must be negative
        (TransactionType.OUTFLOW,  D("1000.00"),    True),   # OUTFLOW must be negative
        (TransactionType.DIVIDEND, D("-200.00"),    True),   # DIVIDEND must be positive
    ])
    def test_sign_parametric(self, txn_type, amount, should_error):
        txns = [Transaction(type=txn_type, amount=amount)]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_signs(result, self._cfg)
        assert was_run is True
        has_error = any(f.severity == ValidationSeverity.ERROR for f in findings)
        assert has_error == should_error, (
            f"Type={txn_type.value} amount={amount}: expected error={should_error}"
        )

    def test_sign_check_skips_zero_amount(self):
        """Zero amount does not trigger a sign error."""
        txns = [Transaction(type=TransactionType.BUY, amount=D("0.00"))]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_signs(result, self._cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_sign_check_skips_none_amount(self):
        txns = [Transaction(type=TransactionType.FEE, amount=None)]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_signs(result, self._cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_sign_check_disabled_by_config(self):
        txns = [Transaction(type=TransactionType.BUY, amount=D("50000.00"))]  # wrong sign
        result = _make_pms_result(transactions=txns)
        cfg = ValidatorConfig(check_transaction_signs=False)
        _, was_run = check_transaction_signs(result, cfg)
        assert was_run is False

    def test_sign_check_skipped_when_no_transactions(self):
        result = _make_pms_result()
        _, was_run = check_transaction_signs(result, self._cfg)
        assert was_run is False


# ---------------------------------------------------------------------------
# 6. ISIN checksum
# ---------------------------------------------------------------------------

class TestISINChecksum:
    _cfg = ValidatorConfig()

    @pytest.mark.parametrize("isin, valid", [
        ("INE002A01018", True),   # Reliance Industries
        ("INE467B01029", True),   # TCS
        ("INE009A01021", True),   # Infosys
        ("INE040A01026", True),   # HDFC Bank
        ("INE090A01021", True),   # ICICI Bank
        ("INE002A01019", False),  # Reliance with wrong check digit
        ("US0378331005", True),   # Apple (US ISIN)
        ("US0378331006", False),  # Apple with bad check digit
        ("INVALIDFORMAT", False), # Too short
        ("INE002A010180", False), # Too long
        ("INE002A0101A",  False), # Check char not digit
    ])
    def test_validate_isin_checksum(self, isin, valid):
        assert validate_isin_checksum(isin) == valid

    def test_all_valid_isins_pass(self):
        holdings = [
            Holding(security_name="Reliance", isin=VALID_ISIN_RELIANCE),
            Holding(security_name="TCS",      isin=VALID_ISIN_TCS),
            Holding(security_name="Infosys",  isin=VALID_ISIN_INFY),
        ]
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_isin_checksums(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_invalid_isin_check_digit(self):
        holdings = [Holding(security_name="Bad Corp", isin="INE002A01019")]
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_isin_checksums(result, self._cfg)
        assert was_run is True
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1
        assert "INE002A01019" in errors[0].message

    def test_invalid_isin_format(self):
        holdings = [Holding(security_name="Weird Corp", isin="TOOLONG12345678")]
        result = _make_pms_result(holdings=holdings)
        findings, _ = check_isin_checksums(result, self._cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1

    def test_none_isin_skipped(self):
        """Holdings with no ISIN are skipped — missing ISIN is a separate concern."""
        holdings = [Holding(security_name="Cash ETF", isin=None)]
        result = _make_pms_result(holdings=holdings)
        findings, was_run = check_isin_checksums(result, self._cfg)
        # was_run = False if no ISINs at all (nothing to check)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert not errors

    def test_isin_check_disabled_by_config(self):
        holdings = [Holding(isin="INE002A01019")]  # bad check digit
        result = _make_pms_result(holdings=holdings)
        cfg = ValidatorConfig(check_isin_checksums=False)
        _, was_run = check_isin_checksums(result, cfg)
        assert was_run is False

    def test_mixed_valid_and_invalid_isins(self):
        holdings = [
            Holding(security_name="Good Corp", isin=VALID_ISIN_RELIANCE),
            Holding(security_name="Bad Corp",  isin="INE002A01019"),  # wrong check digit
        ]
        result = _make_pms_result(holdings=holdings)
        findings, _ = check_isin_checksums(result, self._cfg)
        errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
        assert len(errors) == 1  # only the bad one
        assert "Bad Corp" in errors[0].message or "INE002A01019" in errors[0].message


# ---------------------------------------------------------------------------
# 7. Transaction dates within period
# ---------------------------------------------------------------------------

class TestTransactionDates:
    _cfg = ValidatorConfig()

    def test_all_dates_in_period(self):
        txns = [
            Transaction(type=TransactionType.INFLOW, date=datetime.date(2026, 1, 5)),
            Transaction(type=TransactionType.BUY,    date=datetime.date(2026, 3, 31)),
        ]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_dates(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert not warns

    def test_date_before_period_start(self):
        txns = [
            Transaction(type=TransactionType.INFLOW, date=datetime.date(2025, 12, 31)),
        ]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_dates(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert len(warns) == 1
        assert "transaction_dates" in warns[0].check

    def test_date_after_period_end(self):
        txns = [
            Transaction(type=TransactionType.FEE, date=datetime.date(2026, 4, 1)),
        ]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_dates(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert len(warns) == 1

    def test_date_exactly_on_period_boundaries(self):
        txns = [
            Transaction(type=TransactionType.INFLOW,  date=datetime.date(2026, 1, 1)),
            Transaction(type=TransactionType.OUTFLOW, date=datetime.date(2026, 3, 31)),
        ]
        result = _make_pms_result(transactions=txns)
        findings, _ = check_transaction_dates(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert not warns  # boundary dates are inclusive

    def test_skipped_when_period_not_parseable(self):
        txns = [Transaction(type=TransactionType.INFLOW, date=datetime.date(2026, 1, 1))]
        result = _make_pms_result(transactions=txns, statement_period="Q1 FY26")
        _, was_run = check_transaction_dates(result, self._cfg)
        assert was_run is False  # cannot parse "Q1 FY26"

    def test_skipped_when_no_statement_period(self):
        txns = [Transaction(type=TransactionType.INFLOW, date=datetime.date(2026, 1, 1))]
        result = _make_pms_result(transactions=txns, statement_period=None)
        _, was_run = check_transaction_dates(result, self._cfg)
        assert was_run is False

    def test_string_dates_skipped(self):
        """Dates that remain as strings (unparseable formats) are not compared."""
        txns = [
            Transaction(type=TransactionType.INFLOW, date="Jan 2026"),  # stays as str
        ]
        result = _make_pms_result(transactions=txns)
        findings, was_run = check_transaction_dates(result, self._cfg)
        # was_run may be False if no proper date.date objects were found
        # In any case, no errors/warnings for string dates
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert not warns

    def test_period_formats_parsed(self):
        """Various statement_period string formats are handled."""
        valid_periods = [
            "01 January 2026 to 31 March 2026",
            "January 01, 2026 to March 31, 2026",
            "2026-01-01 to 2026-03-31",
            "01/01/2026 to 31/03/2026",
        ]
        txns = [Transaction(type=TransactionType.INFLOW, date=datetime.date(2026, 2, 15))]
        for period in valid_periods:
            result = _make_pms_result(transactions=txns, statement_period=period)
            _, was_run = check_transaction_dates(result, self._cfg)
            assert was_run is True, f"Period '{period}' should be parseable"

    def test_check_disabled_by_config(self):
        txns = [Transaction(type=TransactionType.INFLOW, date=datetime.date(2025, 1, 1))]
        result = _make_pms_result(transactions=txns)
        cfg = ValidatorConfig(check_transaction_dates=False)
        _, was_run = check_transaction_dates(result, cfg)
        assert was_run is False


# ---------------------------------------------------------------------------
# 8. Identity fields
# ---------------------------------------------------------------------------

class TestIdentityFields:
    _cfg = ValidatorConfig()

    def test_complete_pms_statement_no_warnings(self):
        summary = Summary(
            opening_cash=D("10000"),
            closing_cash=D("20000"),
            opening_value=D("100000"),
            closing_value=D("110000"),
        )
        holdings = [Holding(security_name="Corp A", isin=VALID_ISIN_RELIANCE, weight=D("100"))]
        result = _make_pms_result(holdings=holdings, summary=summary)
        findings, was_run = check_identity_fields(result, self._cfg)
        assert was_run is True
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert not warns

    def test_pms_zero_holdings_warning(self):
        result = _make_pms_result()  # no holdings
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("zero holdings" in w.message.lower() for w in warns)

    def test_missing_client_identifier_warning(self):
        result = _make_pms_result(client_identifier=None)
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("client_identifier" in w.field for w in warns)

    def test_missing_provider_name_warning(self):
        result = _make_pms_result(provider_name=None)
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("provider_name" in w.field for w in warns)

    def test_aif_no_nav_warning(self):
        summary = Summary(closing_value=D("465600.00"))  # no units, no nav_per_unit
        result = _make_aif_result(summary=summary)
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("units" in w.message.lower() or "nav_per_unit" in w.message.lower() for w in warns)

    def test_aif_zero_holdings_ok(self):
        """AIF with no holdings is valid — no warning for empty holdings."""
        summary = Summary(
            closing_value=D("465600.00"),
            units=D("480"),
            nav_per_unit=D("970"),
            opening_cash=D("0"),
            closing_cash=D("462500"),
            opening_value=D("0"),
        )
        result = _make_aif_result(summary=summary)
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        # Should not warn about zero holdings for AIF
        assert not any("zero holdings" in w.message.lower() for w in warns)

    def test_all_monetary_fields_none_warning(self):
        summary = Summary()  # all None
        result = _make_pms_result(summary=summary)
        findings, _ = check_identity_fields(result, self._cfg)
        warns = [f for f in findings if f.severity == ValidationSeverity.WARNING]
        assert any("all primary monetary fields" in w.message.lower() for w in warns)


# ---------------------------------------------------------------------------
# 9. Full Validator.validate integration
# ---------------------------------------------------------------------------

class TestFullValidatorIntegration:
    def test_clean_pms_statement_passes(self):
        """A well-formed PMS statement with reconciled cash and valid ISINs should pass."""
        holdings = [
            Holding(security_name="Reliance", isin=VALID_ISIN_RELIANCE,
                    weight=D("60.00"), market_value=D("375000")),
            Holding(security_name="TCS",      isin=VALID_ISIN_TCS,
                    weight=D("40.00"), market_value=D("250000")),
        ]
        txns = [
            Transaction(type=TransactionType.INFLOW,   amount=D("200000.00"),
                        date=datetime.date(2026, 1, 5)),
            Transaction(type=TransactionType.BUY,      amount=D("-186500.00"),
                        date=datetime.date(2026, 1, 10)),
        ]
        summary = Summary(
            opening_cash=D("80000.00"),
            closing_cash=D("93500.00"),   # 80000 + 200000 - 186500 = 93500
            opening_value=D("500000.00"),
            closing_value=D("718500.00"),
        )
        result = _make_pms_result(holdings=holdings, transactions=txns, summary=summary)
        vr = Validator().validate(result)
        assert vr.passed
        assert vr.final_status == DocumentStatus.PARSED

    def test_cash_error_elevates_to_needs_review(self):
        txns = [Transaction(type=TransactionType.INFLOW, amount=D("100.00"),
                            date=datetime.date(2026, 1, 5))]
        summary = Summary(opening_cash=D("0.00"), closing_cash=D("999.00"))
        result = _make_pms_result(transactions=txns, summary=summary)
        vr = Validator().validate(result)
        assert not vr.passed
        assert vr.final_status == DocumentStatus.NEEDS_REVIEW

    def test_clean_aif_statement_passes(self):
        """AIF with exact NAV identity and balanced cash passes all checks."""
        txns = [
            Transaction(type=TransactionType.INFLOW,  amount=D("500000.00"),
                        date=datetime.date(2026, 1, 2)),
            Transaction(type=TransactionType.OUTFLOW, amount=D("-25000.00"),
                        date=datetime.date(2026, 3, 31)),
            Transaction(type=TransactionType.FEE,     amount=D("-12500.00"),
                        date=datetime.date(2026, 3, 31)),
        ]
        closing_cash = D("500000.00") - D("25000.00") - D("12500.00")  # 462500
        summary = Summary(
            opening_cash=D("0.00"),
            closing_cash=closing_cash,
            opening_value=D("0.00"),
            closing_value=D("465600.00"),
            units=D("480.00"),
            nav_per_unit=D("970.00"),
        )
        result = _make_aif_result(transactions=txns, summary=summary)
        vr = Validator().validate(result)
        # May have mild warnings (e.g. no client_identifier in default fixture)
        # But no errors
        assert vr.passed

    def test_multiple_errors_accumulated(self):
        """Bad ISIN + bad cash → both errors recorded in one call."""
        holdings = [Holding(isin="INE002A01019")]  # bad check digit
        txns = [Transaction(type=TransactionType.INFLOW, amount=D("1.00"),
                            date=datetime.date(2026, 1, 5))]
        summary = Summary(opening_cash=D("0.00"), closing_cash=D("999.00"))
        result = _make_pms_result(holdings=holdings, transactions=txns, summary=summary)
        vr = Validator().validate(result)
        assert not vr.passed
        assert len(vr.errors) >= 2

    def test_checks_run_and_skipped_populated(self):
        """Verify checks_run and checks_skipped are populated correctly."""
        result = _make_pms_result()
        vr = Validator().validate(result)
        assert len(vr.checks_run) > 0
        assert len(vr.checks_skipped) > 0
        # Checks run and skipped are disjoint
        assert not set(vr.checks_run) & set(vr.checks_skipped)


# ---------------------------------------------------------------------------
# 10. ValidatorConfig customisation
# ---------------------------------------------------------------------------

class TestValidatorConfig:
    def test_tighter_cash_tolerance_triggers_error(self):
        """With cash_tolerance=0.01, even 0.50 deviation is an error."""
        txns = [Transaction(type=TransactionType.INFLOW, amount=D("100.00"),
                            date=datetime.date(2026, 1, 5))]
        summary = Summary(opening_cash=D("0.00"), closing_cash=D("100.50"))
        result = _make_pms_result(transactions=txns, summary=summary)

        loose_cfg = ValidatorConfig(cash_tolerance=D("1.00"))
        tight_cfg = ValidatorConfig(cash_tolerance=D("0.01"))

        loose_vr = Validator(loose_cfg).validate(result)
        tight_vr = Validator(tight_cfg).validate(result)

        loose_errors = [f for f in loose_vr.findings if f.severity == ValidationSeverity.ERROR
                       and f.check == "cash_reconciliation"]
        tight_errors = [f for f in tight_vr.findings if f.severity == ValidationSeverity.ERROR
                       and f.check == "cash_reconciliation"]

        assert not loose_errors   # 0.50 ≤ 1.00 → no error
        assert tight_errors       # 0.50 > 0.01 → error

    def test_disable_isin_check(self):
        holdings = [Holding(isin="INE002A01019")]  # bad ISIN
        result = _make_pms_result(holdings=holdings)
        cfg = ValidatorConfig(check_isin_checksums=False)
        vr = Validator(cfg).validate(result)
        isin_errors = [f for f in vr.findings if f.check == "check_isin_checksums"]
        assert not isin_errors

    def test_disable_sign_check(self):
        txns = [Transaction(type=TransactionType.BUY, amount=D("50000.00"),
                            date=datetime.date(2026, 1, 5))]  # wrong sign
        result = _make_pms_result(transactions=txns)
        cfg = ValidatorConfig(check_transaction_signs=False)
        vr = Validator(cfg).validate(result)
        sign_errors = [f for f in vr.findings if "sign" in f.check]
        assert not sign_errors


# ---------------------------------------------------------------------------
# 11. validate_result convenience function
# ---------------------------------------------------------------------------

class TestValidateResultConvenience:
    def test_default_config_used(self):
        result = _make_pms_result()
        vr = validate_result(result)
        assert vr is not None
        assert isinstance(vr.final_status, DocumentStatus)

    def test_custom_config_passed_through(self):
        txns = [Transaction(type=TransactionType.INFLOW, amount=D("100.00"),
                            date=datetime.date(2026, 1, 5))]
        summary = Summary(opening_cash=D("0.00"), closing_cash=D("100.50"))
        result = _make_pms_result(transactions=txns, summary=summary)

        vr_loose = validate_result(result, config=ValidatorConfig(cash_tolerance=D("1.00")))
        vr_tight = validate_result(result, config=ValidatorConfig(cash_tolerance=D("0.01")))

        loose_cash_errors = sum(
            1 for f in vr_loose.findings
            if f.check == "cash_reconciliation" and f.severity == ValidationSeverity.ERROR
        )
        tight_cash_errors = sum(
            1 for f in vr_tight.findings
            if f.check == "cash_reconciliation" and f.severity == ValidationSeverity.ERROR
        )
        assert loose_cash_errors == 0
        assert tight_cash_errors == 1
