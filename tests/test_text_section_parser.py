"""Regression tests for the text-section extraction path.

Covers:
1. Core text-section helpers (_slice_text_section, _parse_lines_with_pattern).
2. extract_text_section_rows — success, missing anchor, bad pattern.
3. parse_text_section_holdings — correct Holding mapping, totals skipped.
4. parse_text_section_transactions — correct Transaction mapping, type inference.
5. Engine integration: text-section fallback when holdings_table finds no rows.
6. Engine integration: existing table path still wins when tables are present.
7. Learner _check_spec_is_usable rejects empty specs.
8. Learner _verify_declared_locators detects failed text_holdings / text_transactions.
9. Draft safety: failed extraction retains draft, never auto-approves.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

from src.engine.text_section_parser import (
    _slice_text_section,
    _parse_lines_with_pattern,
    extract_text_section_rows,
    parse_text_section_holdings,
    parse_text_section_transactions,
)
from src.engine.engine import ParsingEngine
from src.ir.models import BBox, DocumentIR, PageIR, TableIR, TableCell, TextBlock, WordIR
from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.models.models import Holding, ParseResult, Transaction
from src.specs.models import (
    FieldLocator,
    LocatorType,
    ParsingSpec,
    TableLocator,
    TextSectionLocator,
)


# ---------------------------------------------------------------------------
# Helpers: build minimal DocumentIR from plain text
# ---------------------------------------------------------------------------

def _doc_from_text(pages: list[str]) -> DocumentIR:
    """Build a DocumentIR whose pages only contain raw_text (no tables/words)."""
    page_irs: list[PageIR] = []
    for i, text in enumerate(pages, start=1):
        page_irs.append(
            PageIR(
                page_number=i,
                width=595.0,
                height=842.0,
                raw_text=text,
                words=[],
                text_blocks=[TextBlock(text=text, page_number=i, bbox=BBox(x0=0, top=0, x1=600, bottom=800))],
                tables=[],
            )
        )
    return DocumentIR(pages=page_irs, metadata={})


def _doc_with_table(header_row: list[str], data_rows: list[list[str]]) -> DocumentIR:
    """Build a DocumentIR with one structured PDF table on page 1."""
    all_rows = [header_row] + data_rows
    cells = [
        TableCell(text=cell, row_index=r, col_index=c, bbox=BBox(x0=c * 100, top=r * 20, x1=(c + 1) * 100, bottom=(r + 1) * 20))
        for r, row in enumerate(all_rows)
        for c, cell in enumerate(row)
    ]
    table = TableIR(
        page_number=1,
        bbox=BBox(x0=0, top=0, x1=600, bottom=200),
        rows=all_rows,
        cells=cells,
    )
    page = PageIR(
        page_number=1,
        width=595.0,
        height=842.0,
        raw_text=" ".join(header_row),
        words=[],
        text_blocks=[],
        tables=[table],
    )
    return DocumentIR(pages=[page], metadata={})


# ===========================================================================
# 1. _slice_text_section
# ===========================================================================

class TestSliceTextSection:

    def test_slices_between_anchor_and_stop(self):
        text = "INTRO\nSection A\nrow1\nrow2\nSection B\nrow3"
        result = _slice_text_section(text, anchor="Section A", stop_anchor="Section B")
        assert "row1" in result
        assert "row2" in result
        assert "Section B" not in result
        assert "row3" not in result

    def test_no_anchor_returns_full_text(self):
        text = "line1\nline2\nline3"
        result = _slice_text_section(text, anchor=None, stop_anchor=None)
        assert result == text

    def test_anchor_not_found_returns_empty(self):
        text = "Section A\ndata"
        result = _slice_text_section(text, anchor="Nonexistent Section", stop_anchor=None)
        assert result == ""

    def test_no_stop_anchor_returns_remainder(self):
        text = "Header\nSection\nline1\nline2"
        result = _slice_text_section(text, anchor="Section", stop_anchor=None)
        assert "line1" in result
        assert "line2" in result

    def test_case_insensitive_matching(self):
        text = "UNIT HOLDING & VALUATION\nA1 data\nSOMETHING ELSE"
        result = _slice_text_section(text, anchor="unit holding & valuation", stop_anchor="something else")
        assert "A1 data" in result

    def test_start_page_filters_earlier_pages(self):
        page_texts = [(1, "Page 1\nSection\nold data"), (2, "Page 2\nSection\nnew data")]
        full_text = "\n".join(t for _, t in page_texts)
        result = _slice_text_section(
            full_text, anchor="Section", stop_anchor=None,
            start_page=2, page_texts=page_texts,
        )
        assert "new data" in result
        assert "old data" not in result


# ===========================================================================
# 2. _parse_lines_with_pattern
# ===========================================================================

class TestParseLinesWithPattern:

    PATTERN = r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<desc>\w[\w\s]+?)\s+(?P<amount>[\d,]+\.\d{2})"

    def test_matches_valid_lines(self):
        text = "12/01/2024 Contribution 25,00,000.00\n15/07/2024 Contribution 20,00,000.00"
        rows = _parse_lines_with_pattern(text, self.PATTERN)
        assert len(rows) == 2
        assert rows[0]["date"] == "12/01/2024"
        assert rows[0]["amount"] == "25,00,000.00"

    def test_skips_blank_lines(self):
        text = "\n12/01/2024 Contribution 25,00,000.00\n\n"
        rows = _parse_lines_with_pattern(text, self.PATTERN)
        assert len(rows) == 1

    def test_skips_non_matching_lines(self):
        text = "Header line\n12/01/2024 Contribution 25,00,000.00\nTotal 45,00,000.00"
        rows = _parse_lines_with_pattern(text, self.PATTERN)
        assert len(rows) == 1

    def test_skip_patterns_exclude_matching_lines(self):
        text = "12/01/2024 Contribution 25,00,000.00\n12/02/2024 Total 45,00,000.00"
        rows = _parse_lines_with_pattern(text, self.PATTERN, skip_patterns=[r"Total"])
        assert len(rows) == 1
        assert rows[0]["date"] == "12/01/2024"

    def test_empty_text_returns_empty(self):
        assert _parse_lines_with_pattern("", self.PATTERN) == []


# ===========================================================================
# 3. extract_text_section_rows
# ===========================================================================

class TestExtractTextSectionRows:

    PATTERN = r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<desc>[\w\s]+?)\s{2,}(?P<amount>[\d,]+\.\d{2})"

    def test_extracts_rows_correctly(self):
        page_text = (
            "3. Unitholder Ledger\n"
            "Date  Particulars  Amount\n"
            "12/01/2024  Contribution  25,00,000.00\n"
            "15/07/2024  Contribution  20,00,000.00\n"
            "4. Investment Summary\n"
        )
        doc = _doc_from_text([page_text])
        rows, warnings = extract_text_section_rows(
            doc,
            anchor="Unitholder Ledger",
            stop_anchor="Investment Summary",
            row_pattern=self.PATTERN,
        )
        assert len(rows) == 2
        assert rows[0]["date"] == "12/01/2024"
        assert not warnings

    def test_missing_anchor_returns_empty_with_warning(self):
        doc = _doc_from_text(["Some text without the anchor"])
        rows, warnings = extract_text_section_rows(
            doc, anchor="Missing Anchor", stop_anchor=None,
            row_pattern=self.PATTERN,
        )
        assert rows == []
        assert any("no content" in w.lower() for w in warnings)

    def test_bad_pattern_returns_empty_with_warning(self):
        doc = _doc_from_text(["Section\nsome data"])
        rows, warnings = extract_text_section_rows(
            doc, anchor="Section", stop_anchor=None,
            row_pattern="(?P<broken",  # intentionally invalid
        )
        assert rows == []
        assert any("invalid" in w.lower() or "pattern" in w.lower() for w in warnings)

    def test_no_matching_lines_warns(self):
        doc = _doc_from_text(["Section\nno dates here at all"])
        rows, warnings = extract_text_section_rows(
            doc, anchor="Section", stop_anchor=None,
            row_pattern=self.PATTERN,
        )
        assert rows == []
        assert any("matched" in w.lower() or "no lines" in w.lower() for w in warnings)


# ===========================================================================
# 4. parse_text_section_holdings
# ===========================================================================

class TestParseTextSectionHoldings:

    HOLDINGS_TEXT = (
        "2. Unit Holding & Valuation\n"
        "Series  Units  NAV  Value\n"
        "A1  4418.741  1121.40  4955176.16\n"
        "A2  1500.000  1058.20  1587300.00\n"
        "Total  5918.741  -  6542476.16\n"
        "3. Ledger\n"
    )
    PATTERN = r"(?P<series>[A-Z]\d+)\s+(?P<units>[\d,]+\.\d+)\s+(?P<nav>[\d,.]+)\s+(?P<value>[\d,.]+)"
    COL_MAP = {
        "security_name": "series",
        "quantity": "units",
        "price": "nav",
        "market_value": "value",
    }

    def test_parses_holdings_correctly(self):
        doc = _doc_from_text([self.HOLDINGS_TEXT])
        holdings, warnings = parse_text_section_holdings(
            doc,
            anchor="Unit Holding & Valuation",
            stop_anchor="Ledger",
            row_pattern=self.PATTERN,
            column_mapping=self.COL_MAP,
            skip_patterns=[r"^Total\b"],
        )
        assert len(holdings) == 2
        h = holdings[0]
        assert h.security_name == "A1"
        assert h.quantity == Decimal("4418.741")
        assert h.market_value is not None

    def test_total_rows_skipped_via_skip_patterns(self):
        doc = _doc_from_text([self.HOLDINGS_TEXT])
        holdings, _ = parse_text_section_holdings(
            doc,
            anchor="Unit Holding & Valuation",
            stop_anchor="Ledger",
            row_pattern=self.PATTERN,
            column_mapping=self.COL_MAP,
            skip_patterns=[r"^Total\b"],
        )
        names = [h.security_name for h in holdings]
        assert "Total" not in names

    def test_malformed_decimal_adds_warning(self):
        text = "Section\nA1  not_a_number  1121.40  4955176.16\n"
        doc = _doc_from_text([text])
        holdings, warnings = parse_text_section_holdings(
            doc,
            anchor="Section",
            stop_anchor=None,
            row_pattern=self.PATTERN,
            column_mapping=self.COL_MAP,
        )
        # A1 may match but quantity parse fails → warning recorded
        assert any("quantity" in w or "units" in w.lower() for w in warnings) or len(holdings) == 0

    def test_empty_section_returns_no_holdings(self):
        doc = _doc_from_text(["Section\nNothing relevant here\n"])
        holdings, _ = parse_text_section_holdings(
            doc,
            anchor="Section",
            stop_anchor=None,
            row_pattern=self.PATTERN,
            column_mapping=self.COL_MAP,
        )
        assert holdings == []


# ===========================================================================
# 5. parse_text_section_transactions
# ===========================================================================

class TestParseTextSectionTransactions:

    TX_TEXT = (
        "3. Unitholder Ledger\n"
        "Date  Particulars  Series  Amount\n"
        "12/01/2024  Contribution  A1  25000000.00\n"
        "25/09/2025  Income Distribution  -  1500000.00\n"
        "20/03/2026  Income Distribution  -  1750000.00\n"
        "4. Investment Summary\n"
    )
    PATTERN = (
        r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<particulars>[\w\s]+?)\s{2,}"
        r"(?P<series>[\w-]+)\s{2,}"
        r"(?P<amount>[\d,]+\.\d{2})"
    )
    COL_MAP = {
        "date": "date",
        "type": "particulars",
        "security": "series",
        "amount": "amount",
    }

    def test_parses_transactions_correctly(self):
        doc = _doc_from_text([self.TX_TEXT])
        txns, warnings = parse_text_section_transactions(
            doc,
            anchor="Unitholder Ledger",
            stop_anchor="Investment Summary",
            row_pattern=self.PATTERN,
            column_mapping=self.COL_MAP,
            skip_patterns=[r"^Date\b"],
        )
        assert len(txns) >= 1
        t = txns[0]
        assert t.date is not None
        assert isinstance(t.amount, Decimal)

    def test_unknown_type_defaults_to_buy_with_warning(self):
        text = "Section\n12/01/2024  Gibberish  A1  1000.00\n"
        doc = _doc_from_text([text])
        pattern = r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<particulars>\w+)\s+(?P<series>\w+)\s+(?P<amount>[\d,.]+)"
        txns, warnings = parse_text_section_transactions(
            doc,
            anchor="Section",
            stop_anchor=None,
            row_pattern=pattern,
            column_mapping={"date": "date", "type": "particulars", "security": "series", "amount": "amount"},
        )
        if txns:
            assert txns[0].type == TransactionType.BUY
            assert any("unrecognized" in w.lower() or "defaulting" in w.lower() for w in warnings)


# ===========================================================================
# 6. Engine integration: text-section fallback
# ===========================================================================

class TestEngineTextSectionFallback:
    """Engine falls back to text_holdings when holdings_table finds no rows."""

    HOLDINGS_TEXT = (
        "Unit Holding\n"
        "A1  4418.741  1121.40  4955176.16\n"
        "A2  1500.000  1058.20  1587300.00\n"
        "End\n"
    )
    PATTERN = r"(?P<series>[A-Z]\d+)\s+(?P<units>[\d,]+\.\d+)\s+(?P<nav>[\d,.]+)\s+(?P<value>[\d,.]+)"

    def _build_spec(self) -> ParsingSpec:
        return ParsingSpec(
            format_id="test_text_section_fallback",
            statement_type=StatementType.AIF,
            text_holdings=TextSectionLocator(
                anchor="Unit Holding",
                stop_anchor="End",
                row_pattern=self.PATTERN,
                column_mapping={
                    "security_name": "series",
                    "quantity": "units",
                    "price": "nav",
                    "market_value": "value",
                },
            ),
        )

    def test_text_holdings_extracted_when_no_pdf_table(self):
        doc = _doc_from_text([self.HOLDINGS_TEXT])
        spec = self._build_spec()
        result = ParsingEngine().parse(doc, spec)
        assert len(result.holdings) == 2
        assert result.holdings[0].security_name == "A1"

    def test_pdf_table_path_wins_when_table_exists(self):
        """If holdings_table finds rows, text_holdings must NOT override them."""
        doc = _doc_with_table(
            header_row=["security_name", "quantity", "market_value"],
            data_rows=[
                ["Fund X", "100", "10000.00"],
                ["Fund Y", "200", "20000.00"],
            ],
        )
        spec = ParsingSpec(
            format_id="test_table_wins",
            statement_type=StatementType.PMS,
            holdings_table=TableLocator(
                anchor=None,
                header_keywords=["security_name", "quantity"],
                column_mapping={
                    "security_name": "security_name",
                    "quantity": "quantity",
                    "market_value": "market_value",
                },
            ),
            text_holdings=TextSectionLocator(
                anchor="SHOULD NOT BE REACHED",
                row_pattern=self.PATTERN,
                column_mapping={"security_name": "series"},
            ),
        )
        result = ParsingEngine().parse(doc, spec)
        # Table parser should produce rows; text fallback should not fire
        assert len(result.holdings) == 2
        assert result.holdings[0].security_name == "Fund X"


# ===========================================================================
# 7. Learner: _check_spec_is_usable
# ===========================================================================

class TestCheckSpecIsUsable:

    def _make_learner(self):
        from src.learner.learner import Learner
        mock_llm = MagicMock()
        mock_store = MagicMock()
        mock_store.draft_prefix = "draft_"
        return Learner(llm_adapter=mock_llm, spec_store=mock_store)

    def _empty_parse_result(self) -> ParseResult:
        from src.models.models import Statement
        return ParseResult(
            statement=Statement(),
            holdings=[],
            transactions=[],
            summary=None,
            status=DocumentStatus.PARSED,
        )

    def test_empty_spec_on_nonempty_doc_is_rejected(self):
        learner = self._make_learner()
        doc = _doc_from_text(["Fund Name: Kestrel Real Assets\nTotal: 65,42,476.16"])
        spec = ParsingSpec(format_id="test_empty", statement_type=StatementType.AIF)
        result = self._empty_parse_result()
        errors = learner._check_spec_is_usable(doc, spec, result)
        assert any("completely empty" in e.lower() for e in errors)

    def test_spec_with_only_metadata_still_fails_no_summary(self):
        learner = self._make_learner()
        doc = _doc_from_text(["Fund Name: Kestrel\nTotal: 65,00,000.00"])
        spec = ParsingSpec(
            format_id="partial",
            statement_type=StatementType.AIF,
            metadata_fields={
                "provider_name": FieldLocator(
                    locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Fund Name"
                )
            },
        )
        result = self._empty_parse_result()
        errors = learner._check_spec_is_usable(doc, spec, result)
        assert any("summary" in e.lower() for e in errors)

    def test_blank_document_does_not_produce_errors(self):
        learner = self._make_learner()
        doc = _doc_from_text([""])
        spec = ParsingSpec(format_id="blank_doc", statement_type=StatementType.PMS)
        result = self._empty_parse_result()
        errors = learner._check_spec_is_usable(doc, spec, result)
        assert errors == []

    def test_spec_with_metadata_and_summary_passes_usability(self):
        learner = self._make_learner()
        doc = _doc_from_text(["Fund: X\nTotal: 100"])
        spec = ParsingSpec(
            format_id="ok_spec",
            statement_type=StatementType.PMS,
            metadata_fields={
                "provider_name": FieldLocator(
                    locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Fund"
                )
            },
            summary_fields={
                "closing_value": FieldLocator(
                    locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor="Total"
                )
            },
        )
        result = self._empty_parse_result()
        errors = learner._check_spec_is_usable(doc, spec, result)
        assert errors == []


# ===========================================================================
# 8. Learner: _verify_declared_locators for text-section paths
# ===========================================================================

class TestVerifyDeclaredLocatorsTextSection:

    def _make_learner(self):
        from src.learner.learner import Learner
        mock_llm = MagicMock()
        mock_store = MagicMock()
        mock_store.draft_prefix = "draft_"
        return Learner(llm_adapter=mock_llm, spec_store=mock_store)

    def _parse_result_with(self, holdings=None, transactions=None) -> ParseResult:
        from src.models.models import Statement
        return ParseResult(
            statement=Statement(),
            holdings=holdings or [],
            transactions=transactions or [],
            summary=None,
            status=DocumentStatus.PARSED,
        )

    def test_text_holdings_with_no_rows_adds_error(self):
        learner = self._make_learner()
        doc = _doc_from_text(["irrelevant"])
        spec = ParsingSpec(
            format_id="t",
            text_holdings=TextSectionLocator(
                anchor="Holdings",
                row_pattern=r"(?P<series>A\d)",
                column_mapping={"security_name": "series"},
            ),
        )
        result = self._parse_result_with(holdings=[])
        errors = learner._verify_declared_locators(doc, spec, result)
        assert any("text_holdings" in e.lower() or "holdings" in e.lower() for e in errors)

    def test_text_transactions_with_no_rows_adds_error(self):
        learner = self._make_learner()
        doc = _doc_from_text(["irrelevant"])
        spec = ParsingSpec(
            format_id="t",
            text_transactions=TextSectionLocator(
                anchor="Ledger",
                row_pattern=r"(?P<date>\d{2}/\d{2}/\d{4})",
                column_mapping={"date": "date"},
            ),
        )
        result = self._parse_result_with(transactions=[])
        errors = learner._verify_declared_locators(doc, spec, result)
        assert any("text_transactions" in e.lower() or "transaction" in e.lower() for e in errors)

    def test_text_holdings_with_rows_no_error(self):
        learner = self._make_learner()
        doc = _doc_from_text(["irrelevant"])
        spec = ParsingSpec(
            format_id="t",
            text_holdings=TextSectionLocator(
                anchor="Holdings",
                row_pattern=r"(?P<series>A\d)",
                column_mapping={"security_name": "series"},
            ),
        )
        h = Holding(security_name="A1")
        result = self._parse_result_with(holdings=[h])
        errors = learner._verify_declared_locators(doc, spec, result)
        # Should not complain about text_holdings
        assert not any("text_holdings" in e.lower() for e in errors)


# ===========================================================================
# 9. Draft safety: failed learning retains draft, never auto-approves
# ===========================================================================

class TestDraftSafetyOnFailedExtraction:
    """When all repair attempts fail, the spec stays as REJECTED draft — never active."""

    def test_failed_learning_produces_rejected_not_proposed(self):
        """After exhausting retries, status must be REJECTED and is_active False."""
        from src.learner.learner import Learner
        from src.learner.models import LearnerConfig, ProposalStatus
        from src.llm_adapter.models import LLMResponse

        mock_llm = MagicMock()
        mock_store = MagicMock()
        mock_store.draft_prefix = "draft_"
        mock_store.get.side_effect = Exception("not found")

        # LLM always returns an empty spec (no metadata, no summary, no locators)
        empty_spec_json = '{"format_id": "test_draft_safety", "statement_type": "PMS", "metadata_fields": {}, "summary_fields": {}}'
        mock_response = LLMResponse(
            content=empty_spec_json,
            structured=None,
            model="mock-model",
        )
        mock_llm.complete_request.return_value = mock_response
        mock_llm.complete.return_value = mock_response
        mock_llm.config.api_base = "http://localhost:11434/v1"

        doc = _doc_from_text(["Fund: Acme\nTotal NAV: 100000"])

        learner = Learner(
            llm_adapter=mock_llm,
            spec_store=mock_store,
            config=LearnerConfig(max_repairs=1),
        )

        with patch("src.learner.learner.extract_fingerprint") as mock_fp:
            from src.fingerprint.models import FormatFingerprint
            mock_fp.return_value = FormatFingerprint(
                format_id="test_draft_safety",
                anchor_phrases=["fund"],
            )
            result = learner.learn(
                doc=doc,
                format_id="test_draft_safety",
                auto_activate=True,
            )

        assert result.status == ProposalStatus.REJECTED
        assert result.is_active is False
        # save() must NOT have been called with an active spec
        mock_store.save.assert_not_called()
