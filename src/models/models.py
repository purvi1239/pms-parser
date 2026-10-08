from datetime import date as dt_date
from decimal import Decimal
from typing import Any, Union
from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DocumentStatus, StatementType, TransactionType


class Holding(BaseModel):
    """Represents a single portfolio holding/position."""

    model_config = ConfigDict(validate_assignment=True)

    security_name: str | None = None
    isin: str | None = None
    quantity: Decimal | None = None
    cost: Decimal | None = None
    market_value: Decimal | None = None
    weight: Decimal | None = None


class Transaction(BaseModel):
    """Represents a single portfolio cash or security transaction."""

    model_config = ConfigDict(validate_assignment=True)

    date: Union[dt_date, str, None] = None
    type: TransactionType
    security: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None  # Signed cash impact on portfolio


class Summary(BaseModel):
    """Portfolio summary and valuation totals."""

    model_config = ConfigDict(validate_assignment=True)

    opening_value: Decimal | None = None
    closing_value: Decimal | None = None
    opening_cash: Decimal | None = None
    closing_cash: Decimal | None = None
    fees_charged: Decimal | None = None
    income: Decimal | None = None
    realized_gains: Decimal | None = None
    unrealized_gains: Decimal | None = None
    units: Decimal | None = None
    nav_per_unit: Decimal | None = None


class Statement(BaseModel):
    """Top-level investment statement representation."""

    model_config = ConfigDict(validate_assignment=True)

    provider_name: str | None = None
    strategy_name: str | None = None
    client_identifier: str | None = None
    statement_period: str | None = None
    currency: str | None = None
    statement_type: StatementType = StatementType.PMS
    holdings: list[Holding] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    summary: Summary | None = None


class ParseResult(BaseModel):
    """Container for the output of parsing a statement document."""

    model_config = ConfigDict(validate_assignment=True)

    statement: Statement | None = None
    holdings: list[Holding] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    summary: Summary | None = None
    status: DocumentStatus = DocumentStatus.PARSED
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

