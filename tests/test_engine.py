"""Tests for Step 7: Deterministic Parsing Engine.

Covers:
  1. Supported field locator types:
     - VALUE_RIGHT_OF_ANCHOR (from key/value table cells and from inline text)
     - VALUE_BELOW_ANCHOR (from tables and from spatial text lines)
     - VALUE_MATCHING_PATTERN (regex extraction)
     - BOUNDING_BOX (absolute coordinate fallback)
  2. Fallback bounding box on anchor-relative locators when anchor is absent.
  3. TABLE_AFTER_ANCHOR table parsing:
     - Header row detection
     - Dynamic column mapping
     - Stop anchor termination
     - Multi-page table continuation and repeated header row suppression
  4. Missing optional fields and default values.
  5. Missing required fields recording warnings and PARSED_WITH_WARNINGS status.
  6. Malformed quantitative values (graceful handling and warning recording).
  7. Page number constraint filtering.
  8. End-to-end parsing of synthetic PMS PDF statements (Format A and Format A-messy).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import pytest

from src.engine import (
    ParsingEngine,
    extract_field_value,
    parse_document,
    to_decimal,
    to_date,
    to_transaction_type,
)
from src.ir import extract_pdf
from src.ir.models import (
    BBox,
    DocumentIR,
    PageIR,
    TableIR,
    TextBlock,
    WordIR,
)
from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.specs.models import (
    FieldLocator,
    LocatorType,
    ParsingSpec,
    TableLocator,
)


# ---------------------------------------------------------------------------
# Fixtures: In-Memory DocumentIR for Fast, Targeted Engine Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_document_ir() -> DocumentIR:
    """Construct an in-memory DocumentIR with typical PMS layout structures."""
    # Page 1: Header, Meta Table, Summary, and Page 1 Holdings
    p1_words = [
        WordIR(text="Portfolio", page_number=1, bbox=BBox(x0=50, top=30, x1=90, bottom=42)),
        WordIR(text="Management", page_number=1, bbox=BBox(x0=95, top=30, x1=150, bottom=42)),
        WordIR(text="Statement", page_number=1, bbox=BBox(x0=155, top=30, x1=200, bottom=42)),
        # Meta block
        WordIR(text="Client", page_number=1, bbox=BBox(x0=50, top=60, x1=80, bottom=72)),
        WordIR(text="ID:", page_number=1, bbox=BBox(x0=82, top=60, x1=95, bottom=72)),
        WordIR(text="PWM-CLIENT-0042", page_number=1, bbox=BBox(x0=105, top=60, x1=200, bottom=72)),
        # Special BBox region for fallback testing
        WordIR(text="CONFIDENTIAL-TAG", page_number=1, bbox=BBox(x0=400, top=10, x1=500, bottom=25)),
    ]

    p1_blocks = [
        TextBlock(text="Portfolio Management Statement", page_number=1, bbox=BBox(x0=50, top=30, x1=200, bottom=42)),
        TextBlock(text="Pinnacle Wealth Management — India Growth Strategy", page_number=1, bbox=BBox(x0=50, top=45, x1=300, bottom=55)),
        TextBlock(text="Client ID: PWM-CLIENT-0042  Statement Period: Q1 2026", page_number=1, bbox=BBox(x0=50, top=60, x1=350, bottom=72)),
        TextBlock(text="Holdings", page_number=1, bbox=BBox(x0=50, top=90, x1=100, bottom=105)),
    ]

    # Meta Table
    meta_table = TableIR(
        page_number=1,
        rows=[
            ["Client ID:", "PWM-CLIENT-0042", "Statement Period:", "Q1 2026"],
            ["Currency:", "INR", "Statement Type:", "PMS"],
        ],
    )

    # Holdings Table (Page 1)
    holdings_p1 = TableIR(
        page_number=1,
        rows=[
            ["Security Name", "ISIN", "Qty", "Cost (INR)", "Mkt Value (INR)", "Weight %"],
            ["Reliance Industries Ltd", "INE002A01018", "100", "240,000.00", "290,000.00", "20.5%"],
            ["Tata Consultancy Services", "INE467B01029", "50", "180,000.00", "210,000.00", "15.0%"],
        ],
    )

    page1 = PageIR(
        page_number=1,
        width=595.0,
        height=842.0,
        raw_text="Portfolio Management Statement\nPinnacle Wealth Management — India Growth Strategy\nClient ID: PWM-CLIENT-0042  Statement Period: Q1 2026\nCurrency: INR  Statement Type: PMS\nHoldings\n",
        words=p1_words,
        text_blocks=p1_blocks,
        tables=[meta_table, holdings_p1],
    )

    # Page 2: Continuation of Holdings with Repeated Header, then Transactions, then Summary
    p2_blocks = [
        TextBlock(text="Holdings (Contd.)", page_number=2, bbox=BBox(x0=50, top=30, x1=150, bottom=45)),
        TextBlock(text="Transactions", page_number=2, bbox=BBox(x0=50, top=200, x1=120, bottom=215)),
        TextBlock(text="Portfolio Summary", page_number=2, bbox=BBox(x0=50, top=400, x1=150, bottom=415)),
    ]

    holdings_p2 = TableIR(
        page_number=2,
        rows=[
            # Repeated header row
            ["Security Name", "ISIN", "Qty", "Cost (INR)", "Mkt Value (INR)", "Weight %"],
            ["Infosys Ltd", "INE009A01021", "80", "120,000.00", "145,000.00", "10.2%"],
            ["Total Holdings", "", "230", "540,000.00", "645,000.00", "45.7%"],
        ],
    )

    txns_table = TableIR(
        page_number=2,
        rows=[
            ["Date", "Type", "Security", "Qty", "Amount (INR)"],
            ["15/01/2026", "buy", "Reliance Industries Ltd", "20", "58,000.00"],
            ["22/02/2026", "dividend", "Tata Consultancy Services", "", "1,500.00"],
        ],
    )

    summary_table = TableIR(
        page_number=2,
        rows=[
            ["Opening Portfolio Value", "₹1,200,000.00"],
            ["Closing Portfolio Value", "₹1,450,000.00"],
            ["Opening Cash Balance", "₹80,000.00"],
            ["Closing Cash Balance", "₹120,000.00"],
            ["Management Fees", "₹5,000.00"],
        ],
    )

    page2 = PageIR(
        page_number=2,
        width=595.0,
        height=842.0,
        raw_text="Holdings\nTransactions\nPortfolio Summary\nOpening Portfolio Value ₹1,200,000.00\nClosing Portfolio Value ₹1,450,000.00\n",
        words=[],
        text_blocks=p2_blocks,
        tables=[holdings_p2, txns_table, summary_table],
    )

    return DocumentIR(
        source_path="/fake/statement.pdf",
        page_count=2,
        pages=[page1, page2],
        metadata={"title": "Test Statement"},
    )


# ---------------------------------------------------------------------------
# 1. Field Locators Unit Tests
# ---------------------------------------------------------------------------

def test_value_right_of_anchor_extraction(mock_document_ir):
    """VALUE_RIGHT_OF_ANCHOR extracts text from tables and inline blocks."""
    # From meta table
    loc_client = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Client ID:",
    )
    val, warns = extract_field_value(mock_document_ir, loc_client, "client_id")
    assert val == "PWM-CLIENT-0042"
    assert not warns

    # From currency row
    loc_curr = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Currency:",
    )
    val_curr, _ = extract_field_value(mock_document_ir, loc_curr, "currency")
    assert val_curr == "INR"


def test_value_right_of_anchor_single_space_adjacent_label():
    """VALUE_RIGHT_OF_ANCHOR strips adjacent labels when separated by single space."""
    block1 = TextBlock(
        text="Client ID: PWM-CLIENT-0042 Statement Period: 01 January 2026 to 31 March 2026",
        page_number=1,
        bbox=BBox(x0=50, top=60, x1=450, bottom=72),
    )
    block2 = TextBlock(
        text="Currency: INR Statement Type: PMS Quarterly Portfolio Report",
        page_number=1,
        bbox=BBox(x0=50, top=75, x1=400, bottom=87),
    )
    block3 = TextBlock(
        text="Account Number: ACT-0029-B Report Period: 01 January 2026 to 31 March 2026",
        page_number=1,
        bbox=BBox(x0=50, top=90, x1=450, bottom=102),
    )
    block4 = TextBlock(
        text="Investor ID: NV-AIF-2026-0011 Commitment Period: 01 January 2026 to 31 March 2026",
        page_number=1,
        bbox=BBox(x0=50, top=105, x1=450, bottom=117),
    )
    block5 = TextBlock(
        text="Client Name: John Doe Statement Period: 01 January 2026 to 31 March 2026",
        page_number=1,
        bbox=BBox(x0=50, top=120, x1=450, bottom=132),
    )
    block6 = TextBlock(
        text="Strategy: Large Cap Equity / Growth Statement Period: 01 January 2026 to 31 March 2026",
        page_number=1,
        bbox=BBox(x0=50, top=135, x1=450, bottom=147),
    )
    block7 = TextBlock(
        text="Report Time: 10:30:00 Status: Final Statement Period: 01 Jan 2026",
        page_number=1,
        bbox=BBox(x0=50, top=150, x1=450, bottom=162),
    )
    block8 = TextBlock(
        text="Realised Gains: ₹0.00 Unrealised Gains: ₹174,000.00",
        page_number=1,
        bbox=BBox(x0=50, top=165, x1=450, bottom=177),
    )
    doc = DocumentIR(
        page_count=1,
        pages=[PageIR(
            page_number=1,
            width=595.0,
            height=842.0,
            text_blocks=[block1, block2, block3, block4, block5, block6, block7, block8],
        )],
    )

    # Format A Client ID
    val_a, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Client ID:"),
        "client_identifier",
    )
    assert val_a == "PWM-CLIENT-0042"

    # Format A Currency
    val_curr, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Currency:"),
        "currency",
    )
    assert val_curr == "INR"

    # Format B Account Number
    val_b, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Account Number:"),
        "client_identifier",
    )
    assert val_b == "ACT-0029-B"

    # Format C Investor ID
    val_c, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Investor ID:"),
        "client_identifier",
    )
    assert val_c == "NV-AIF-2026-0011"

    # Multi-word string without truncation
    val_name, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Client Name:"),
        "client_name",
    )
    assert val_name == "John Doe"

    # Multi-word strategy with slashes without truncation
    val_strat, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Strategy:"),
        "strategy",
    )
    assert val_strat == "Large Cap Equity / Growth"

    # Value containing colon (time)
    val_time, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Report Time:"),
        "report_time",
    )
    assert val_time == "10:30:00"

    # Legitimate zero currency value
    val_zero, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Realised Gains:"),
        "realized_gains",
    )
    assert val_zero == "₹0.00"


def test_value_right_of_anchor_legitimate_zero_and_missing_values():
    """Verify that zero values are extracted faithfully and missing values return None/default."""
    table = TableIR(
        page_number=1,
        rows=[
            ["Realised Gains", "0.00"],
            ["Fees Waived", "0"],
            ["Unrealised Gains", "₹0.00"],
            ["Empty Value", ""],
            ["Whitespace Value", "   "],
        ],
    )
    doc = DocumentIR(page_count=1, pages=[PageIR(page_number=1, width=595.0, height=842.0, tables=[table])])

    # Legitimate zero strings must be preserved
    val_rg, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Realised Gains"))
    assert val_rg == "0.00"

    val_fw, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Fees Waived"))
    assert val_fw == "0"

    val_ug, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Unrealised Gains"))
    assert val_ug == "₹0.00"

    # Truly empty or whitespace-only cells return None
    val_empty, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Empty Value"))
    assert val_empty is None

    val_ws, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Whitespace Value"))
    assert val_ws is None

    # Missing anchor with explicit default returns the default
    val_default, _ = extract_field_value(
        doc,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Nonexistent Field", default=Decimal("0.00")),
        "nonexistent",
    )
    assert val_default == Decimal("0.00")


def test_value_right_of_anchor_false_matches_prevention():
    """Verify that search does not match unrelated compound labels or partial substring anchors."""
    table = TableIR(
        page_number=1,
        rows=[
            ["Total Portfolio Value", "₹1,500,000.00"],
            ["Total", "₹1,200,000.00"],
            ["Opening Portfolio Value", "₹1,000,000.00"],
            ["Income Tax", "₹5,000.00"],
            ["Income (Dividends/Int.)", "₹12,000.00"],
            ["NAV Date", "31/03/2026"],
            ["NAV Per Unit (INR)", "₹970.00"],
        ],
    )
    block_text = TextBlock(
        text="This portfolio has Fixed Income: ₹50,000.00. Normal Income: ₹12,000.00.",
        page_number=1,
        bbox=BBox(x0=50, top=200, x1=450, bottom=212),
    )
    doc = DocumentIR(
        page_count=1,
        pages=[PageIR(page_number=1, width=595.0, height=842.0, tables=[table], text_blocks=[block_text])],
    )

    # Exact 'Total' should match 'Total', not 'Total Portfolio Value'
    val_total, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Total"))
    assert val_total == "₹1,200,000.00"

    # 'Income' should match 'Income (Dividends/Int.)', not 'Income Tax'
    val_inc, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Income"))
    assert val_inc == "₹12,000.00"

    # 'Opening' should NOT match 'Opening Portfolio Value'
    val_open, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Opening"))
    assert val_open is None

    # 'NAV' should NOT match 'NAV Date'
    val_nav, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="NAV"))
    assert val_nav is None


def test_value_right_of_anchor_parenthetical_prefix_and_negative_amounts():
    """Verify parenthetical unit stripping while preserving legitimate negative amounts in parentheses."""
    block1 = TextBlock(
        text="Income (Dividends/Int.): ₹12,000.00",
        page_number=1,
        bbox=BBox(x0=50, top=60, x1=400, bottom=72),
    )
    block2 = TextBlock(
        text="Opening Balance (INR): ₹500,000.00",
        page_number=1,
        bbox=BBox(x0=50, top=75, x1=400, bottom=87),
    )
    block3 = TextBlock(
        text="Net Realised Loss: (1,250.00)",
        page_number=1,
        bbox=BBox(x0=50, top=90, x1=400, bottom=102),
    )
    block4 = TextBlock(
        text="Adjustment: (0.00)",
        page_number=1,
        bbox=BBox(x0=50, top=105, x1=400, bottom=117),
    )
    doc = DocumentIR(
        page_count=1,
        pages=[PageIR(page_number=1, width=595.0, height=842.0, text_blocks=[block1, block2, block3, block4])],
    )

    # Unit / description parenthetical stripped
    val_inc, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Income"))
    assert val_inc == "₹12,000.00"

    val_open, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Opening Balance"))
    assert val_open == "₹500,000.00"

    # Negative accounting amounts in parentheses MUST be preserved
    val_loss, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Net Realised Loss:"))
    assert val_loss == "(1,250.00)"

    val_adj, _ = extract_field_value(doc, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Adjustment:"))
    assert val_adj == "(0.00)"


def test_summary_field_extractions_formats_a_b_c():
    """Verify summary extraction across vertical, horizontal, and AIF multi-table formats."""
    # 1. Format A vertical table with prefix anchor (e.g. Income matching Income (Dividends/Int.))
    table_a = TableIR(
        page_number=1,
        rows=[
            ["Opening Portfolio Value", "₹1,200,000.00"],
            ["Closing Portfolio Value", "₹1,473,500.00"],
            ["Income (Dividends/Int.)", "₹12,000.00"],
        ],
    )
    doc_a = DocumentIR(page_count=1, pages=[PageIR(page_number=1, width=595.0, height=842.0, tables=[table_a])])
    val_open, _ = extract_field_value(doc_a, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Opening Portfolio Value"))
    assert val_open == "₹1,200,000.00"
    val_inc, _ = extract_field_value(doc_a, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Income"))
    assert val_inc == "₹12,000.00"

    # 2. Format B horizontal summary table (VALUE_BELOW_ANCHOR)
    table_b = TableIR(
        page_number=1,
        rows=[
            ["Opening Value", "Closing Value", "Opening Cash", "Closing Cash", "Fees Charged", "Income", "Realised Gains", "Unrealised Gains"],
            ["₹1,200,000.00", "₹1,473,500.00", "₹80,000.00", "₹93,500.00", "₹8,500.00", "₹12,000.00", "₹0.00", "₹174,000.00"],
        ],
    )
    doc_b = DocumentIR(page_count=1, pages=[PageIR(page_number=1, width=595.0, height=842.0, tables=[table_b])])
    val_b_open, _ = extract_field_value(doc_b, FieldLocator(locator_type=LocatorType.VALUE_BELOW_ANCHOR, anchor="Opening Value"))
    assert val_b_open == "₹1,200,000.00"
    val_b_cash, _ = extract_field_value(doc_b, FieldLocator(locator_type=LocatorType.VALUE_BELOW_ANCHOR, anchor="Opening Cash"))
    assert val_b_cash == "₹80,000.00"
    val_b_gains, _ = extract_field_value(doc_b, FieldLocator(locator_type=LocatorType.VALUE_BELOW_ANCHOR, anchor="Unrealised Gains"))
    assert val_b_gains == "₹174,000.00"

    # 3. Format C AIF summary tables (Fund Overview & Fund Performance)
    table_c_nav = TableIR(
        page_number=1,
        rows=[
            ["Units Held", "480.0000"],
            ["NAV Per Unit (INR)", "₹970.00"],
            ["Total Investment Value (INR)", "₹465,600.00"],
        ],
    )
    doc_c = DocumentIR(page_count=1, pages=[PageIR(page_number=1, width=595.0, height=842.0, tables=[table_c_nav])])
    val_c_units, _ = extract_field_value(doc_c, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Units Held"))
    assert val_c_units == "480.0000"
    val_c_nav, _ = extract_field_value(doc_c, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="NAV Per Unit"))
    assert val_c_nav == "₹970.00"
    val_c_close, _ = extract_field_value(doc_c, FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Total Investment Value"))
    assert val_c_close == "₹465,600.00"


def test_actual_pdf_extraction_formats_a_b_c():
    """Verify actual PDF extraction across Formats A, B, and C using extract_pdf."""
    samples_dir = Path("samples/synthetic")
    pdf_a = samples_dir / "format_a1.pdf"
    if not (samples_dir.is_dir() and pdf_a.exists()):
        pytest.skip("Synthetic PDF samples not found in samples/synthetic")

    # 1. Format A PDF extraction
    doc_a = extract_pdf(pdf_a)
    assert doc_a.page_count >= 1
    val_client_a, _ = extract_field_value(
        doc_a,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Client ID:"),
        "client_identifier",
    )
    assert val_client_a == "PWM-CLIENT-0042"

    val_curr_a, _ = extract_field_value(
        doc_a,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Currency:"),
        "currency",
    )
    assert val_curr_a == "INR"

    val_open_a, _ = extract_field_value(
        doc_a,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Opening Portfolio Value", offset_x=500.0),
        "opening_value",
    )
    assert val_open_a is not None and "₹" in val_open_a

    val_inc_a, _ = extract_field_value(
        doc_a,
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Income", offset_x=500.0),
        "income",
    )
    assert val_inc_a is not None and "₹" in val_inc_a

    # 2. Format B PDF extraction
    pdf_b = samples_dir / "format_b.pdf"
    if pdf_b.exists():
        doc_b = extract_pdf(pdf_b)
        val_acc_b, _ = extract_field_value(
            doc_b,
            FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Account Number:"),
            "client_identifier",
        )
        assert val_acc_b == "HCM-2025-0099"

        val_curr_b, _ = extract_field_value(
            doc_b,
            FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Base Currency:"),
            "currency",
        )
        assert val_curr_b == "INR"

        # Format B horizontal summary table uses VALUE_BELOW_ANCHOR
        val_open_b, _ = extract_field_value(
            doc_b,
            FieldLocator(locator_type=LocatorType.VALUE_BELOW_ANCHOR, anchor="Opening Value"),
            "opening_value",
        )
        assert val_open_b is not None and "₹" in val_open_b

    # 3. Format C PDF extraction (AIF)
    pdf_c = samples_dir / "format_c.pdf"
    if pdf_c.exists():
        doc_c = extract_pdf(pdf_c)
        val_inv_c, _ = extract_field_value(
            doc_c,
            FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Investor ID:"),
            "client_identifier",
        )
        assert val_inv_c == "NV-AIF-2026-0011"

        val_units_c, _ = extract_field_value(
            doc_c,
            FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Units Held", offset_x=500.0),
            "units",
        )
        assert val_units_c is not None and "480" in val_units_c


def test_value_below_anchor_extraction():
    """VALUE_BELOW_ANCHOR extracts values positioned vertically beneath anchor."""
    # Create simple table with header above value
    table = TableIR(
        page_number=1,
        rows=[
            ["Strategy Name", "Report Date"],
            ["India Growth Strategy", "31/03/2026"],
        ],
    )
    doc = DocumentIR(
        page_count=1,
        pages=[PageIR(page_number=1, width=500, height=500, tables=[table])],
    )

    loc = FieldLocator(
        locator_type=LocatorType.VALUE_BELOW_ANCHOR,
        anchor="Strategy Name",
    )
    val, warns = extract_field_value(doc, loc, "strategy_name")
    assert val == "India Growth Strategy"
    assert not warns


def test_value_matching_pattern_extraction(mock_document_ir):
    """VALUE_MATCHING_PATTERN extracts text matching regex pattern."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_MATCHING_PATTERN,
        pattern=r"Statement Period:\s*([A-Za-z0-9\s]+)",
    )
    val, warns = extract_field_value(mock_document_ir, loc, "statement_period")
    assert "Q1 2026" in val
    assert not warns


def test_bounding_box_extraction(mock_document_ir):
    """BOUNDING_BOX extracts words within coordinate boundaries."""
    loc = FieldLocator(
        locator_type=LocatorType.BOUNDING_BOX,
        bbox=BBox(x0=390, top=5, x1=510, bottom=30),
        page_number=1,
    )
    val, warns = extract_field_value(mock_document_ir, loc, "tag")
    assert val == "CONFIDENTIAL-TAG"
    assert not warns


def test_fallback_bounding_box_when_anchor_missing(mock_document_ir):
    """Anchor locator falls back to fallback_bbox when anchor is missing."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Nonexistent Anchor Key:",
        fallback_bbox=BBox(x0=390, top=5, x1=510, bottom=30),
        page_number=1,
    )
    val, warns = extract_field_value(mock_document_ir, loc, "fallback_field")
    assert val == "CONFIDENTIAL-TAG"
    assert len(warns) == 1
    assert "extracted value using fallback bounding box" in warns[0]


# ---------------------------------------------------------------------------
# 2. Table Extraction & Multi-Page / Repeated Headers Tests
# ---------------------------------------------------------------------------

def test_holdings_table_multi_page_and_stop_anchor(mock_document_ir):
    """Holdings table spans multiple pages, skips repeated headers, and stops at stop_anchor."""
    spec = ParsingSpec(
        format_id="test_format_a",
        statement_type=StatementType.PMS,
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Holdings",
            header_keywords=["Security Name", "ISIN", "Qty", "Cost"],
            column_mapping={
                "security_name": "Security Name",
                "isin": "ISIN",
                "quantity": "Qty",
                "cost": "Cost (INR)",
                "market_value": "Mkt Value (INR)",
                "weight": "Weight %",
            },
            stop_anchor="Total Holdings",
            multi_page=True,
            repeat_headers=True,
        ),
    )

    res = parse_document(mock_document_ir, spec)
    assert res.status == DocumentStatus.PARSED
    # Expected 3 securities (2 from page 1, 1 from page 2; repeated header and Total Holdings ignored)
    assert len(res.holdings) == 3

    h1 = res.holdings[0]
    assert h1.security_name == "Reliance Industries Ltd"
    assert h1.isin == "INE002A01018"
    assert h1.quantity == Decimal("100")
    assert h1.cost == Decimal("240000.00")
    assert h1.market_value == Decimal("290000.00")
    assert h1.weight == Decimal("20.5")

    h2 = res.holdings[1]
    assert h2.security_name == "Tata Consultancy Services"
    assert h2.isin == "INE467B01029"

    h3 = res.holdings[2]
    assert h3.security_name == "Infosys Ltd"
    assert h3.isin == "INE009A01021"
    assert h3.quantity == Decimal("80")


def test_transactions_table_extraction(mock_document_ir):
    """Transactions table extracts trade date, activity, security, quantity, and amount."""
    spec = ParsingSpec(
        format_id="test_format_a",
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Transactions",
            header_keywords=["Date", "Type", "Security", "Qty", "Amount"],
            column_mapping={
                "date": "Date",
                "type": "Type",
                "security": "Security",
                "quantity": "Qty",
                "amount": "Amount (INR)",
            },
        ),
    )

    res = parse_document(mock_document_ir, spec)
    assert len(res.transactions) == 2

    t1 = res.transactions[0]
    assert str(t1.date) == "2026-01-15"
    assert t1.type == TransactionType.BUY
    assert t1.security == "Reliance Industries Ltd"
    assert t1.quantity == Decimal("20")
    assert t1.amount == Decimal("58000.00")

    t2 = res.transactions[1]
    assert str(t2.date) == "2026-02-22"
    assert t2.type == TransactionType.DIVIDEND
    assert t2.quantity is None
    assert t2.amount == Decimal("1500.00")


# ---------------------------------------------------------------------------
# 3. Missing Fields & Malformed Value Resilience Tests
# ---------------------------------------------------------------------------

def test_missing_optional_fields_use_defaults(mock_document_ir):
    """Optional fields that cannot be located default cleanly to None or spec default."""
    spec = ParsingSpec(
        format_id="test_format",
        metadata_fields={
            "optional_broker": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Broker:",
                required=False,
            ),
            "currency_with_default": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Nonexistent Currency Key:",
                required=False,
                default="USD",
            ),
        },
    )

    res = parse_document(mock_document_ir, spec)
    assert res.statement.provider_name is None
    assert res.status == DocumentStatus.PARSED
    assert not res.warnings


def test_missing_required_field_adds_warning(mock_document_ir):
    """Missing required field emits warning and updates status to PARSED_WITH_WARNINGS."""
    spec = ParsingSpec(
        format_id="test_format",
        metadata_fields={
            "pan_number": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="PAN Number:",
                required=True,
            )
        },
    )

    res = parse_document(mock_document_ir, spec)
    assert res.status == DocumentStatus.PARSED_WITH_WARNINGS
    assert any("Required metadata field 'pan_number'" in w for w in res.warnings)


def test_malformed_decimal_values_gracefully_handled():
    """Malformed numbers in summary or holdings record warnings rather than crashing."""
    table = TableIR(
        page_number=1,
        rows=[
            ["Security Name", "Qty", "Cost"],
            ["Test Corp", "INVALID_QTY", "12,345.67"],
        ],
    )
    doc = DocumentIR(
        page_count=1,
        pages=[PageIR(page_number=1, width=500, height=500, tables=[table])],
    )

    spec = ParsingSpec(
        format_id="test_malformed",
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            header_keywords=["Security Name", "Qty"],
            column_mapping={"security_name": "Security Name", "quantity": "Qty", "cost": "Cost"},
        ),
        summary_fields={
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Nonexistent Value:",
                default="NOT_A_DECIMAL",
            )
        },
    )

    res = parse_document(doc, spec)
    assert res.status == DocumentStatus.PARSED_WITH_WARNINGS
    assert len(res.holdings) == 1
    assert res.holdings[0].security_name == "Test Corp"
    assert res.holdings[0].quantity is None  # Malformed quantity gracefully set to None
    assert res.holdings[0].cost == Decimal("12345.67")
    assert any("Cannot convert" in w for w in res.warnings)


def test_page_number_constraint(mock_document_ir):
    """Locators specifying page_number only evaluate that page."""
    # Anchor "Holdings" is on page 1 and page 2, but page 1 has no summary
    loc_wrong_page = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Closing Portfolio Value",
        page_number=1,  # summary is on page 2
    )
    val, _ = extract_field_value(mock_document_ir, loc_wrong_page, "val")
    assert val is None

    loc_right_page = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Closing Portfolio Value",
        page_number=2,
    )
    val2, _ = extract_field_value(mock_document_ir, loc_right_page, "val")
    assert val2 is not None


# ---------------------------------------------------------------------------
# 4. Type Converters Unit Tests
# ---------------------------------------------------------------------------

def test_decimal_conversion_formats():
    """Verify conversion of various monetary and accounting number formats."""
    assert to_decimal("₹1,250,000.50") == Decimal("1250000.50")
    assert to_decimal("$ 450.25") == Decimal("450.25")
    assert to_decimal("15.75 %") == Decimal("15.75")
    assert to_decimal("(50,000.00)") == Decimal("-50000.00")
    assert to_decimal("-1,200.00") == Decimal("-1200.00")
    assert to_decimal("₹-110,000.00") == Decimal("-110000.00")
    assert to_decimal("-₹50,000.00") == Decimal("-50000.00")
    assert to_decimal("₹(12,500.00)") == Decimal("-12500.00")
    assert to_decimal("(₹12,500.00)") == Decimal("-12500.00")
    assert to_decimal("₹-25,000.00") == Decimal("-25000.00")
    assert to_decimal("0.00") == Decimal("0.00")
    assert to_decimal("₹0.00") == Decimal("0.00")
    assert to_decimal("N/A") is None
    assert to_decimal("-") is None
    assert to_decimal("—") is None
    assert to_decimal("–") is None
    assert to_decimal("₹ —") is None

    with pytest.raises(ValueError):
        to_decimal("corrupted_amount_xyz")


def test_date_conversion_formats():
    """Verify parsing of standard statement date formats."""
    d1 = to_date("15/01/2026")
    assert str(d1) == "2026-01-15"

    d2 = to_date("2026-03-31")
    assert str(d2) == "2026-03-31"

    d3 = to_date("15 Jan 2026")
    assert str(d3) == "2026-01-15"


def test_transaction_type_conversion():
    """Verify mapping of activity strings to TransactionType."""
    assert to_transaction_type("BUY") == TransactionType.BUY
    assert to_transaction_type("purchase") == TransactionType.BUY
    assert to_transaction_type("Subscription") == TransactionType.BUY
    assert to_transaction_type("Sale") == TransactionType.SELL
    assert to_transaction_type("Div Payout") == TransactionType.DIVIDEND
    assert to_transaction_type("Management Fee") == TransactionType.FEE
    assert to_transaction_type("Funds Added") == TransactionType.INFLOW
    assert to_transaction_type("Withdrawal") == TransactionType.OUTFLOW
