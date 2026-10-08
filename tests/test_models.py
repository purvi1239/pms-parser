from datetime import date
from decimal import Decimal
import pytest
from pydantic import ValidationError

from src.models import (
    DocumentStatus,
    Holding,
    ParseResult,
    Statement,
    StatementType,
    Summary,
    Transaction,
    TransactionType,
)


def test_model_creation():
    """Verify clean instantiation of all primary models."""
    holding = Holding(
        security_name="Reliance Industries Ltd",
        isin="INE002A01018",
        quantity=Decimal("100"),
        cost=Decimal("2400.50"),
        market_value=Decimal("2600.00"),
        weight=Decimal("15.5"),
    )
    assert holding.security_name == "Reliance Industries Ltd"
    assert holding.isin == "INE002A01018"
    assert holding.quantity == Decimal("100")

    txn = Transaction(
        date=date(2026, 3, 15),
        type=TransactionType.BUY,
        security="TCS",
        quantity=Decimal("10"),
        amount=Decimal("-35000.00"),
    )
    assert txn.type == TransactionType.BUY
    assert txn.amount == Decimal("-35000.00")

    summary = Summary(
        opening_value=Decimal("1000000.00"),
        closing_value=Decimal("1050000.00"),
        opening_cash=Decimal("50000.00"),
        closing_cash=Decimal("15000.00"),
        units=Decimal("1000"),
        nav_per_unit=Decimal("1050.00"),
    )
    assert summary.opening_value == Decimal("1000000.00")
    assert summary.closing_value == Decimal("1050000.00")
    assert summary.units == Decimal("1000")

    statement = Statement(
        provider_name="Alpha Capital PMS",
        strategy_name="Large Cap Growth",
        client_identifier="CLI-9876",
        statement_period="2026-01-01 to 2026-03-31",
        currency="INR",
        statement_type=StatementType.PMS,
        holdings=[holding],
        transactions=[txn],
        summary=summary,
    )
    assert statement.provider_name == "Alpha Capital PMS"
    assert len(statement.holdings) == 1
    assert len(statement.transactions) == 1

    result = ParseResult(
        statement=statement,
        holdings=[holding],
        transactions=[txn],
        summary=summary,
        status=DocumentStatus.PARSED,
    )
    assert result.status == DocumentStatus.PARSED
    assert len(result.holdings) == 1
    assert len(result.transactions) == 1
    assert result.summary == summary



def test_decimal_values():
    """Ensure monetary and financial numbers are strictly stored as Decimal without float drift."""
    # From string
    h1 = Holding(market_value="1250000.75", quantity="500.5")
    assert isinstance(h1.market_value, Decimal)
    assert h1.market_value == Decimal("1250000.75")
    assert isinstance(h1.quantity, Decimal)
    assert h1.quantity == Decimal("500.5")

    # From int
    t1 = Transaction(type=TransactionType.INFLOW, amount=100000)
    assert isinstance(t1.amount, Decimal)
    assert t1.amount == Decimal("100000")

    # From float: Pydantic v2 preserves clean decimal precision
    s1 = Summary(closing_value=12345.67)
    assert isinstance(s1.closing_value, Decimal)
    assert s1.closing_value == Decimal("12345.67")

    # Negative cash impact for buy
    t2 = Transaction(type=TransactionType.BUY, amount=Decimal("-4500.25"))
    assert t2.amount < Decimal("0")


def test_default_empty_holdings():
    """Verify that holdings defaults to an empty list and each instance gets an independent list."""
    stmt1 = Statement()
    stmt2 = Statement()
    assert stmt1.holdings == []
    assert isinstance(stmt1.holdings, list)

    res1 = ParseResult()
    res2 = ParseResult()
    assert res1.holdings == []
    assert isinstance(res1.holdings, list)

    # Mutability check: appending to one does not mutate the other
    stmt1.holdings.append(Holding(security_name="Test Stock"))
    assert len(stmt1.holdings) == 1
    assert len(stmt2.holdings) == 0

    res1.holdings.append(Holding(security_name="Test Stock 2"))
    assert len(res1.holdings) == 1
    assert len(res2.holdings) == 0


def test_enum_status_values():
    """Verify all 11 DocumentStatus members and case-insensitive parsing."""
    expected_statuses = {
        "DUPLICATE",
        "NEEDS_PASSWORD",
        "SKIPPED",
        "MAYBE",
        "CLASSIFIED",
        "PARSED",
        "PARSED_WITH_WARNINGS",
        "NEEDS_REVIEW",
        "DRIFT_SUSPECTED",
        "LEARNING_FAILED",
        "FAILED",
    }
    actual_statuses = {s.value for s in DocumentStatus}
    assert actual_statuses == expected_statuses

    # Case-insensitivity in DocumentStatus
    assert DocumentStatus("parsed") == DocumentStatus.PARSED
    assert DocumentStatus("DRIFT_SUSPECTED") == DocumentStatus.DRIFT_SUSPECTED
    assert DocumentStatus("learning_failed") == DocumentStatus.LEARNING_FAILED

    # StatementType checks
    assert StatementType.PMS == "PMS"
    assert StatementType.AIF == "AIF"
    assert StatementType("pms") == StatementType.PMS
    assert StatementType("aif") == StatementType.AIF

    # TransactionType checks
    assert TransactionType.BUY == "buy"
    assert TransactionType.SELL == "sell"
    assert TransactionType.DIVIDEND == "dividend"
    assert TransactionType.FEE == "fee"
    assert TransactionType.INFLOW == "inflow"
    assert TransactionType.OUTFLOW == "outflow"
    assert TransactionType("BUY") == TransactionType.BUY
    assert TransactionType("Dividend") == TransactionType.DIVIDEND


def test_basic_pydantic_validation():
    """Verify Pydantic validation failures and default None values."""
    # Summary optional fields must default to None
    summary = Summary()
    assert summary.opening_value is None
    assert summary.closing_value is None
    assert summary.opening_cash is None
    assert summary.closing_cash is None
    assert summary.fees_charged is None
    assert summary.income is None
    assert summary.realized_gains is None
    assert summary.unrealized_gains is None
    assert summary.units is None
    assert summary.nav_per_unit is None

    # Invalid decimal string raises ValidationError
    with pytest.raises(ValidationError):
        Holding(market_value="invalid-number")

    # Invalid transaction type raises ValidationError
    with pytest.raises(ValidationError):
        Transaction(type="unsupported_transaction_type")

    # Invalid document status raises ValidationError
    with pytest.raises(ValidationError):
        ParseResult(status="UNKNOWN_STATUS")

    # Serialization roundtrip
    holding = Holding(security_name="HDFC Bank", quantity=Decimal("50"))
    dump = holding.model_dump()
    assert dump["security_name"] == "HDFC Bank"
    assert dump["quantity"] == Decimal("50")

    # Statement currency must default to None (no assumption of INR)
    stmt = Statement()
    assert stmt.currency is None

    # ParseResult simple container behavior
    result = ParseResult(
        statement=Statement(provider_name="Test Provider"),
        holdings=[holding],
        status=DocumentStatus.PARSED_WITH_WARNINGS,
        warnings=["No identity checkable"],
    )
    assert result.statement is not None
    assert result.statement.provider_name == "Test Provider"
    assert result.holdings == [holding]
    assert result.transactions == []
    assert result.summary is None
    assert result.status == DocumentStatus.PARSED_WITH_WARNINGS
    assert result.warnings == ["No identity checkable"]
    assert result.errors == []
    assert result.raw_metadata == {}



def test_json_roundtrip_and_precision():
    """Verify JSON serialization/deserialization retains exact Decimal precision."""
    precise_val = Decimal("123456789.987654321")
    summary = Summary(
        opening_value=precise_val,
        closing_value=precise_val * Decimal("2"),
        units=Decimal("1234.5678"),
        nav_per_unit=Decimal("100.1234"),
    )
    json_str = summary.model_dump_json()
    restored = Summary.model_validate_json(json_str)
    assert restored.opening_value == precise_val
    assert restored.closing_value == Decimal("246913579.975308642")
    assert restored.units == Decimal("1234.5678")


def test_aif_structure():
    """Verify AIF statements can have empty holdings with units and NAV."""
    aif_summary = Summary(
        opening_value=Decimal("5000000.00"),
        closing_value=Decimal("5200000.00"),
        units=Decimal("5000.00"),
        nav_per_unit=Decimal("1040.00"),
    )
    aif_stmt = Statement(
        provider_name="Nexus Ventures AIF",
        strategy_name="Tech Growth Fund II",
        statement_type=StatementType.AIF,
        holdings=[],
        summary=aif_summary,
    )
    assert aif_stmt.statement_type == StatementType.AIF
    assert aif_stmt.holdings == []
    assert aif_stmt.summary.nav_per_unit == Decimal("1040.00")

