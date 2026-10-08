"""Canonical synthetic financial data for all sample formats.

All money values are Python Decimal.  Financial consistency is enforced at
construction time so ground-truth JSON is always valid.

Sign convention (Plan v4):
  + : cash enters portfolio  (inflow, sell proceeds, dividend/interest)
  - : cash leaves portfolio  (buy, fee, outflow)

Cash identity: closing_cash = opening_cash + sum(signed transaction amounts)
Value identity: closing = opening + inflows - outflows + income - fees ± gains
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from src.sample_generator._isin import SYNTHETIC_ISINS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Q2 = Decimal("0.01")   # 2-decimal rounding
Q4 = Decimal("0.0001") # 4-decimal rounding


def d(s: str | int | float) -> Decimal:
    return Decimal(str(s))


def pct(numerator: Decimal, total: Decimal) -> Decimal:
    """Return percentage weight rounded to 4 d.p."""
    if total == 0:
        return Decimal("0")
    return (numerator / total * d(100)).quantize(Q4, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class HoldingData:
    security_name: str
    isin: str
    quantity: Decimal
    cost: Decimal          # total cost (not per-share)
    market_value: Decimal
    weight: Decimal = field(init=False)  # set by parent after computing total

    def set_weight(self, total_market_value: Decimal) -> None:
        self.weight = pct(self.market_value, total_market_value)


@dataclass
class TransactionData:
    date: date
    txn_type: str        # TransactionType value string
    security: str | None
    quantity: Decimal | None
    amount: Decimal      # signed cash impact


@dataclass
class SummaryData:
    opening_value: Decimal
    closing_value: Decimal
    opening_cash: Decimal
    closing_cash: Decimal
    fees_charged: Decimal
    income: Decimal
    realized_gains: Decimal
    unrealized_gains: Decimal
    # AIF-only fields
    units: Decimal | None = None
    nav_per_unit: Decimal | None = None

    def verify_cash_identity(self, transactions: Sequence[TransactionData]) -> None:
        """Raise AssertionError if cash identity does not hold."""
        txn_sum = sum(t.amount for t in transactions)
        expected = (self.opening_cash + txn_sum).quantize(Q2, rounding=ROUND_HALF_UP)
        actual = self.closing_cash.quantize(Q2, rounding=ROUND_HALF_UP)
        assert expected == actual, (
            f"Cash identity violation: {self.opening_cash} + {txn_sum} = "
            f"{expected} ≠ closing_cash {actual}"
        )

    def verify_nav_identity(self) -> None:
        """Raise AssertionError if AIF NAV identity does not hold within 1%."""
        if self.units is None or self.nav_per_unit is None:
            return
        implied = (self.units * self.nav_per_unit).quantize(Q2, rounding=ROUND_HALF_UP)
        tol = (self.closing_value * d("0.01")).quantize(Q2, rounding=ROUND_HALF_UP)
        diff = abs(implied - self.closing_value)
        assert diff <= tol, (
            f"NAV identity violation: {self.units} × {self.nav_per_unit} = "
            f"{implied} vs closing_value {self.closing_value} (diff={diff})"
        )


@dataclass
class SampleData:
    label: str
    provider_name: str
    strategy_name: str
    client_identifier: str
    statement_period: str
    period_start: date
    period_end: date
    currency: str
    statement_type: str  # "PMS" or "AIF"
    holdings: list[HoldingData]
    transactions: list[TransactionData]
    summary: SummaryData

    def verify(self) -> None:
        self.summary.verify_cash_identity(self.transactions)
        self.summary.verify_nav_identity()
        if self.holdings:
            total_weight = sum(h.weight for h in self.holdings)
            assert abs(total_weight - d(100)) <= d("0.1"), (
                f"Weights sum {total_weight} ≠ 100% for {self.label}"
            )


# ---------------------------------------------------------------------------
# Format A — Normal PMS statement (two instances: A1, A2)
# ---------------------------------------------------------------------------

def _make_format_a1() -> SampleData:
    holdings = [
        HoldingData("Reliance Industries Limited", SYNTHETIC_ISINS["RELIANCE"],
                    d("150"), d("330000"), d("375000")),
        HoldingData("Tata Consultancy Services Limited", SYNTHETIC_ISINS["TCS"],
                    d("80"),  d("280000"), d("312000")),
        HoldingData("Infosys Limited", SYNTHETIC_ISINS["INFY"],
                    d("200"), d("290000"), d("320000")),
        HoldingData("HDFC Bank Limited", SYNTHETIC_ISINS["HDFC_BANK"],
                    d("120"), d("180000"), d("204000")),
        HoldingData("ICICI Bank Limited", SYNTHETIC_ISINS["ICICI_BANK"],
                    d("200"), d("150000"), d("169000")),
    ]
    total_mv = sum(h.market_value for h in holdings)
    for h in holdings:
        h.set_weight(total_mv)
    # Force weights to exactly 100% by adjusting last
    weight_sum = sum(h.weight for h in holdings)
    holdings[-1].weight += (d(100) - weight_sum)

    opening_cash = d("80000.00")
    transactions = [
        TransactionData(date(2026, 1, 5),  "inflow",  None,        None,      d("200000.00")),
        TransactionData(date(2026, 1, 10), "buy",     "Reliance Industries Limited", d("50"),  d("-110000.00")),
        TransactionData(date(2026, 2, 15), "buy",     "Tata Consultancy Services Limited", d("20"),  d("-78000.00")),
        TransactionData(date(2026, 3, 1),  "sell",    "Infosys Limited", d("30"),  d("48000.00")),
        TransactionData(date(2026, 3, 20), "dividend",None,        None,      d("12000.00")),
        TransactionData(date(2026, 3, 28), "fee",     None,        None,      d("-8500.00")),
        TransactionData(date(2026, 3, 31), "outflow", None,        None,      d("-50000.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    summary = SummaryData(
        opening_value=d("1200000.00"),
        closing_value=total_mv + closing_cash,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("8500.00"),
        income=d("12000.00"),
        realized_gains=d("5000.00"),
        unrealized_gains=(total_mv - sum(h.cost for h in holdings)).quantize(Q2),
    )
    data = SampleData(
        label="format_a1",
        provider_name="Pinnacle Wealth PMS",
        strategy_name="India Growth Strategy",
        client_identifier="PWM-CLIENT-0042",
        statement_period="01 January 2026 to 31 March 2026",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        currency="INR",
        statement_type="PMS",
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


def _make_format_a2() -> SampleData:
    """Second Format-A statement (Q4 2025) — different data, same layout."""
    holdings = [
        HoldingData("Reliance Industries Limited",   SYNTHETIC_ISINS["RELIANCE"],
                    d("200"), d("420000"), d("490000")),
        HoldingData("Wipro Limited",                 SYNTHETIC_ISINS["WIPRO"],
                    d("500"), d("225000"), d("260000")),
        HoldingData("Larsen & Toubro Limited",       SYNTHETIC_ISINS["LT"],
                    d("100"), d("310000"), d("360000")),
        HoldingData("Bajaj Finance Limited",         SYNTHETIC_ISINS["BAJAJ_FIN"],
                    d("40"),  d("280000"), d("330000")),
    ]
    total_mv = sum(h.market_value for h in holdings)
    for h in holdings:
        h.set_weight(total_mv)
    weight_sum = sum(h.weight for h in holdings)
    holdings[-1].weight += (d(100) - weight_sum)

    opening_cash = d("60000.00")
    transactions = [
        TransactionData(date(2025, 10, 3),  "inflow",   None,     None,   d("150000.00")),
        TransactionData(date(2025, 10, 15), "buy",      "Wipro Limited", d("200"), d("-90000.00")),
        TransactionData(date(2025, 11, 20), "sell",     "Bajaj Finance Limited", d("10"), d("72000.00")),
        TransactionData(date(2025, 12, 10), "dividend", None,     None,   d("9500.00")),
        TransactionData(date(2025, 12, 31), "fee",      None,     None,   d("-7200.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    summary = SummaryData(
        opening_value=d("1050000.00"),
        closing_value=total_mv + closing_cash,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("7200.00"),
        income=d("9500.00"),
        realized_gains=d("8000.00"),
        unrealized_gains=(total_mv - sum(h.cost for h in holdings)).quantize(Q2),
    )
    data = SampleData(
        label="format_a2",
        provider_name="Pinnacle Wealth PMS",
        strategy_name="India Growth Strategy",
        client_identifier="PWM-CLIENT-0042",
        statement_period="01 October 2025 to 31 December 2025",
        period_start=date(2025, 10, 1),
        period_end=date(2025, 12, 31),
        currency="INR",
        statement_type="PMS",
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


# ---------------------------------------------------------------------------
# Format B — Structurally different PMS (different headings/table order)
# ---------------------------------------------------------------------------

def _make_format_b() -> SampleData:
    holdings = [
        HoldingData("Maruti Suzuki India Limited",   SYNTHETIC_ISINS["MARUTI"],
                    d("60"),  d("540000"), d("618000")),
        HoldingData("Axis Bank Limited",             SYNTHETIC_ISINS["AXIS_BANK"],
                    d("300"), d("210000"), d("252000")),
        HoldingData("Sun Pharmaceutical Inds. Ltd",  SYNTHETIC_ISINS["SUNPHARMA"],
                    d("250"), d("275000"), d("310000")),
    ]
    total_mv = sum(h.market_value for h in holdings)
    for h in holdings:
        h.set_weight(total_mv)
    weight_sum = sum(h.weight for h in holdings)
    holdings[-1].weight += (d(100) - weight_sum)

    opening_cash = d("40000.00")
    transactions = [
        TransactionData(date(2026, 1, 8),  "inflow",   None,      None,     d("100000.00")),
        TransactionData(date(2026, 2, 5),  "buy",      "Maruti Suzuki India Limited", d("20"),  d("-180000.00")),
        TransactionData(date(2026, 3, 15), "dividend", None,      None,     d("15000.00")),
        TransactionData(date(2026, 3, 31), "fee",      None,      None,     d("-6000.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    summary = SummaryData(
        opening_value=d("1100000.00"),
        closing_value=total_mv + closing_cash,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("6000.00"),
        income=d("15000.00"),
        realized_gains=d("0.00"),
        unrealized_gains=(total_mv - sum(h.cost for h in holdings)).quantize(Q2),
    )
    data = SampleData(
        label="format_b",
        provider_name="Horizon Capital Management",
        strategy_name="Diversified Equity Portfolio",
        client_identifier="HCM-2025-0099",
        statement_period="January 01, 2026 to March 31, 2026",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        currency="INR",
        statement_type="PMS",
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


# ---------------------------------------------------------------------------
# Format C — AIF Statement
# ---------------------------------------------------------------------------

def _make_format_c() -> SampleData:
    # AIF: no individual security holdings; investors hold "units"
    opening_cash = d("0.00")
    transactions = [
        TransactionData(date(2026, 1, 2),  "inflow",   None, None, d("500000.00")),
        TransactionData(date(2026, 3, 31), "outflow",  None, None, d("-25000.00")),
        TransactionData(date(2026, 3, 31), "fee",      None, None, d("-12500.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    units = d("480.00")
    nav_per_unit = d("970.00")  # ≈ closing_value / units
    closing_value = (units * nav_per_unit).quantize(Q2)

    summary = SummaryData(
        opening_value=d("0.00"),
        closing_value=closing_value,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("12500.00"),
        income=d("0.00"),
        realized_gains=d("0.00"),
        unrealized_gains=d("0.00"),
        units=units,
        nav_per_unit=nav_per_unit,
    )
    data = SampleData(
        label="format_c",
        provider_name="Nexus Ventures AIF",
        strategy_name="Tech Growth Fund II (Category II AIF)",
        client_identifier="NV-AIF-2026-0011",
        statement_period="01 January 2026 to 31 March 2026",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        currency="INR",
        statement_type="AIF",
        holdings=[],   # AIF: zero holdings is valid
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


# ---------------------------------------------------------------------------
# A-messy: same data as A1 but with extended holdings across pages
# ---------------------------------------------------------------------------

def _make_format_a_messy() -> SampleData:
    """Format A with 10 holdings so they must span multiple pages.

    Same structural anchors as Format A — engine should parse with A's spec.
    One security name is intentionally long (wrapped).
    """
    holdings = [
        HoldingData("Reliance Industries Limited",                               SYNTHETIC_ISINS["RELIANCE"],   d("150"), d("330000"), d("375000")),
        HoldingData("Tata Consultancy Services Limited",                         SYNTHETIC_ISINS["TCS"],        d("80"),  d("280000"), d("312000")),
        HoldingData("Infosys Limited",                                           SYNTHETIC_ISINS["INFY"],       d("200"), d("290000"), d("320000")),
        HoldingData("HDFC Bank Limited",                                         SYNTHETIC_ISINS["HDFC_BANK"],  d("120"), d("180000"), d("204000")),
        HoldingData("ICICI Bank Limited",                                        SYNTHETIC_ISINS["ICICI_BANK"], d("200"), d("150000"), d("169000")),
        HoldingData("Wipro Limited",                                             SYNTHETIC_ISINS["WIPRO"],      d("300"), d("135000"), d("156000")),
        HoldingData("Larsen & Toubro Limited",                                   SYNTHETIC_ISINS["LT"],         d("60"),  d("186000"), d("216000")),
        HoldingData("Bajaj Finance Limited",                                     SYNTHETIC_ISINS["BAJAJ_FIN"],  d("30"),  d("210000"), d("247500")),
        # Deliberately long name to force wrapping in PDF
        HoldingData("Maruti Suzuki India Limited (Automobile Manufacturer)",     SYNTHETIC_ISINS["MARUTI"],     d("40"),  d("360000"), d("412000")),
        HoldingData("Axis Bank Limited",                                         SYNTHETIC_ISINS["AXIS_BANK"],  d("250"), d("175000"), d("210000")),
    ]
    total_mv = sum(h.market_value for h in holdings)
    for h in holdings:
        h.set_weight(total_mv)
    weight_sum = sum(h.weight for h in holdings)
    holdings[-1].weight += (d(100) - weight_sum)

    opening_cash = d("80000.00")
    transactions = [
        TransactionData(date(2026, 1, 5),  "inflow",   None,        None,  d("200000.00")),
        TransactionData(date(2026, 1, 10), "buy",     "Reliance Industries Limited",    d("50"),  d("-110000.00")),
        TransactionData(date(2026, 2, 15), "buy",     "Tata Consultancy Services Limited", d("20"), d("-78000.00")),
        TransactionData(date(2026, 3, 1),  "sell",    "Infosys Limited", d("30"), d("48000.00")),
        TransactionData(date(2026, 3, 20), "dividend", None,        None,  d("12000.00")),
        TransactionData(date(2026, 3, 28), "fee",      None,        None,  d("-8500.00")),
        TransactionData(date(2026, 3, 31), "outflow",  None,        None,  d("-50000.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    summary = SummaryData(
        opening_value=d("2100000.00"),
        closing_value=total_mv + closing_cash,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("8500.00"),
        income=d("12000.00"),
        realized_gains=d("5000.00"),
        unrealized_gains=(total_mv - sum(h.cost for h in holdings)).quantize(Q2),
    )
    data = SampleData(
        label="format_a_messy",
        provider_name="Pinnacle Wealth PMS",
        strategy_name="India Growth Strategy",
        client_identifier="PWM-CLIENT-0042",
        statement_period="01 January 2026 to 31 March 2026",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        currency="INR",
        statement_type="PMS",
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


# ---------------------------------------------------------------------------
# A-drift: structurally mutated Format A — triggers DRIFT_SUSPECTED
# ---------------------------------------------------------------------------

def _make_format_a_drift() -> SampleData:
    """Mutated Format A: renamed column header, reordered columns, changed date format.

    Data values are valid; layout is different enough for fingerprint drift detection.
    Same provider/client as A1 to simulate a real quarter-end reformat.
    """
    holdings = [
        HoldingData("Reliance Industries Limited",   SYNTHETIC_ISINS["RELIANCE"],  d("150"), d("330000"), d("375000")),
        HoldingData("Tata Consultancy Services Ltd", SYNTHETIC_ISINS["TCS"],       d("80"),  d("280000"), d("312000")),
        HoldingData("Infosys Limited",               SYNTHETIC_ISINS["INFY"],      d("200"), d("290000"), d("320000")),
        HoldingData("HDFC Bank Limited",             SYNTHETIC_ISINS["HDFC_BANK"], d("120"), d("180000"), d("204000")),
        HoldingData("ICICI Bank Limited",            SYNTHETIC_ISINS["ICICI_BANK"],d("200"), d("150000"), d("169000")),
    ]
    total_mv = sum(h.market_value for h in holdings)
    for h in holdings:
        h.set_weight(total_mv)
    weight_sum = sum(h.weight for h in holdings)
    holdings[-1].weight += (d(100) - weight_sum)

    opening_cash = d("80000.00")
    transactions = [
        TransactionData(date(2026, 1, 5),  "inflow",   None,        None,  d("200000.00")),
        TransactionData(date(2026, 1, 10), "buy",     "Reliance Industries Limited",    d("50"),  d("-110000.00")),
        TransactionData(date(2026, 3, 20), "dividend", None,        None,  d("12000.00")),
        TransactionData(date(2026, 3, 28), "fee",      None,        None,  d("-8500.00")),
    ]
    txn_sum = sum(t.amount for t in transactions)
    closing_cash = (opening_cash + txn_sum).quantize(Q2)

    summary = SummaryData(
        opening_value=d("1200000.00"),
        closing_value=total_mv + closing_cash,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        fees_charged=d("8500.00"),
        income=d("12000.00"),
        realized_gains=d("0.00"),
        unrealized_gains=(total_mv - sum(h.cost for h in holdings)).quantize(Q2),
    )
    data = SampleData(
        label="format_a_drift",
        provider_name="Pinnacle Wealth PMS",
        strategy_name="India Growth Strategy",
        client_identifier="PWM-CLIENT-0042",
        statement_period="01 January 2026 to 31 March 2026",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        currency="INR",
        statement_type="PMS",
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )
    data.verify()
    return data


# ---------------------------------------------------------------------------
# Public accessor
# ---------------------------------------------------------------------------

def get_all_samples() -> dict[str, SampleData]:
    return {
        "format_a1":      _make_format_a1(),
        "format_a2":      _make_format_a2(),
        "format_b":       _make_format_b(),
        "format_c":       _make_format_c(),
        "format_a_messy": _make_format_a_messy(),
        "format_a_drift": _make_format_a_drift(),
    }
