"""Tests for Step 17: Parser Evaluation Framework.

Covers:
1. Field comparator: correct matches across strings, numbers, dates, and enums.
2. Field comparator: value discrepancies and mismatches (INCORRECT).
3. Field comparator: missing expected fields (MISSING).
4. Field comparator: unexpected actual fields (UNEXPECTED).
5. Field comparator: distinguishing legitimate zero, False, and empty values from missing values.
6. Decimal and monetary tolerance handling without float inaccuracy.
7. Date format normalization (ISO strings vs datetime/date objects).
8. Single case evaluation with mock pipeline.
9. Aggregate metric calculations (cases, fields, accuracies, denominators).
10. Performance breakdown by statement type (PMS vs AIF).
11. Pipeline failure isolation without halting evaluation batch.
12. Empty evaluation set handled without division-by-zero errors.
13. Human-readable text report and machine-readable JSON report generation and file export.
14. Deterministic reproducibility across repeated runs.
15. Loading evaluation cases from ground-truth JSON files.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.eval import (
    CaseEvaluationResult,
    EvaluationCase,
    EvaluationConfig,
    EvaluationResult,
    Evaluator,
    FieldComparisonResult,
    FieldMatchStatus,
    compare_field_value,
    evaluate_cases,
    extract_field_value,
    load_case_from_ground_truth,
    load_cases_from_directory,
    to_date_str,
    to_decimal,
)
from src.ingestion.folder import PipelineInterface
from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.models.models import Holding, ParseResult, Statement, Summary, Transaction
from src.pipeline.models import PipelineResult


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def _build_dummy_statement(
    provider_name: str = "Pinnacle Wealth PMS",
    strategy_name: str = "India Growth Strategy",
    client_identifier: str = "PWM-0042",
    statement_period: str = "01 Jan 2026 to 31 Mar 2026",
    statement_type: StatementType = StatementType.PMS,
    closing_value: Decimal = Decimal("1473500.00"),
    closing_cash: Decimal = Decimal("93500.00"),
    holdings_count: int = 2,
) -> Statement:
    summary = Summary(
        opening_value=Decimal("1200000.00"),
        closing_value=closing_value,
        opening_cash=Decimal("80000.00"),
        closing_cash=closing_cash,
        fees_charged=Decimal("8500.00"),
        income=Decimal("12000.00"),
        realized_gains=Decimal("5000.00"),
        unrealized_gains=Decimal("150000.00"),
    )
    holdings = [
        Holding(security_name="Reliance Industries", isin="INE002A01018", quantity=Decimal("150"), market_value=Decimal("375000")),
        Holding(security_name="TCS Limited", isin="INE467B01029", quantity=Decimal("80"), market_value=Decimal("312000")),
    ][:holdings_count]

    return Statement(
        provider_name=provider_name,
        strategy_name=strategy_name,
        client_identifier=client_identifier,
        statement_period=statement_period,
        currency="INR",
        statement_type=statement_type,
        summary=summary,
        holdings=holdings,
    )


def _make_pipeline_result(statement: Statement | None, status: DocumentStatus = DocumentStatus.PARSED) -> PipelineResult:
    return PipelineResult(
        document_id="doc_eval_test",
        status=status,
        file_path="eval_doc.pdf",
        file_hash="dummy_hash",
        parse_result=ParseResult(statement=statement, status=status) if statement else None,
    )


# ---------------------------------------------------------------------------
# Tests: Field-level comparison and Normalizations
# ---------------------------------------------------------------------------

class TestFieldComparator:
    """Tests for compare_field_value and type normalizations."""

    def test_correct_matches(self):
        """Values that match exactly or within tolerance return FieldMatchStatus.CORRECT."""
        # String match
        r1 = compare_field_value("provider", "Kotak Wealth", "Kotak Wealth")
        assert r1.status == FieldMatchStatus.CORRECT

        # Exact Decimal match
        r2 = compare_field_value("val", Decimal("100.50"), Decimal("100.50"))
        assert r2.status == FieldMatchStatus.CORRECT

        # String to Decimal conversion
        r3 = compare_field_value("val", "100.50", Decimal("100.50"))
        assert r3.status == FieldMatchStatus.CORRECT

        # Enum to string match
        r4 = compare_field_value("type", StatementType.PMS, "PMS")
        assert r4.status == FieldMatchStatus.CORRECT

        # Date normalization
        r5 = compare_field_value("date", "2026-01-01", date(2026, 1, 1))
        assert r5.status == FieldMatchStatus.CORRECT

    def test_incorrect_values(self):
        """Discrepancies return FieldMatchStatus.INCORRECT with descriptive difference."""
        r1 = compare_field_value("provider", "Kotak Wealth", "HDFC Wealth")
        assert r1.status == FieldMatchStatus.INCORRECT
        assert "Kotak Wealth" in r1.difference
        assert "HDFC Wealth" in r1.difference

        # Decimal discrepancy beyond tolerance
        r2 = compare_field_value("cash", Decimal("1000.00"), Decimal("1005.00"))
        assert r2.status == FieldMatchStatus.INCORRECT
        assert "Numeric discrepancy" in r2.difference

        # Date mismatch
        r3 = compare_field_value("date", "2026-01-01", "2026-02-01")
        assert r3.status == FieldMatchStatus.INCORRECT

    def test_missing_expected_fields(self):
        """When expected value is defined but actual is None, returns FieldMatchStatus.MISSING."""
        r1 = compare_field_value("closing_val", Decimal("500.00"), None)
        assert r1.status == FieldMatchStatus.MISSING
        assert "missing or None" in r1.difference

        r2 = compare_field_value("provider", "Pinnacle", None, is_extracted=False)
        assert r2.status == FieldMatchStatus.MISSING

    def test_unexpected_fields(self):
        """When expected is None but actual has a value, returns FieldMatchStatus.UNEXPECTED."""
        r = compare_field_value("optional_field", None, "Extracted Value")
        assert r.status == FieldMatchStatus.UNEXPECTED
        assert "Expected None" in r.difference

    def test_distinguish_legitimate_zero_and_false_from_missing(self):
        """Distinguishes genuine 0, False, and empty strings from missing/None."""
        # Genuine 0 matches 0
        r_zero = compare_field_value("fees", Decimal("0.00"), Decimal("0.00"))
        assert r_zero.status == FieldMatchStatus.CORRECT

        # Genuine 0 does NOT match None
        r_zero_missing = compare_field_value("fees", Decimal("0.00"), None)
        assert r_zero_missing.status == FieldMatchStatus.MISSING

        # Genuine False matches False
        r_false = compare_field_value("is_active", False, False)
        assert r_false.status == FieldMatchStatus.CORRECT

        # Genuine False does NOT match None
        r_false_missing = compare_field_value("is_active", False, None)
        assert r_false_missing.status == FieldMatchStatus.MISSING

        # Genuine empty string matches empty string
        r_empty = compare_field_value("notes", "", "")
        assert r_empty.status == FieldMatchStatus.CORRECT

        # Genuine empty string does NOT match None
        r_empty_missing = compare_field_value("notes", "", None)
        assert r_empty_missing.status == FieldMatchStatus.MISSING

    def test_decimal_monetary_tolerance_policy(self):
        """Tolerances are evaluated with exact Decimal arithmetic without floating-point inaccuracy."""
        cfg = EvaluationConfig(monetary_tolerance=Decimal("0.05"))

        # Diff is 0.04 <= 0.05 -> CORRECT
        r_pass = compare_field_value("cash", Decimal("100.00"), Decimal("100.04"), config=cfg)
        assert r_pass.status == FieldMatchStatus.CORRECT

        # Diff is 0.06 > 0.05 -> INCORRECT
        r_fail = compare_field_value("cash", Decimal("100.00"), Decimal("100.06"), config=cfg)
        assert r_fail.status == FieldMatchStatus.INCORRECT

        # Zero tolerance strictly requires exact Decimal match
        cfg_zero = EvaluationConfig(monetary_tolerance=Decimal("0.00"))
        r_exact = compare_field_value("cash", Decimal("100.00"), Decimal("100.01"), config=cfg_zero)
        assert r_exact.status == FieldMatchStatus.INCORRECT

    def test_date_normalization(self):
        """Normalizes date strings, date objects, and datetime objects."""
        assert to_date_str(date(2026, 3, 31)) == "2026-03-31"
        assert to_date_str(datetime(2026, 3, 31, 15, 30)) == "2026-03-31"
        assert to_date_str("2026-03-31") == "2026-03-31"
        assert to_date_str("invalid_date") is None

    def test_extract_field_value_dot_paths(self):
        """extract_field_value navigates nested objects and synthetic count attributes."""
        stmt = _build_dummy_statement(closing_value=Decimal("9999.00"))

        found, val = extract_field_value(stmt, "provider_name")
        assert found is True
        assert val == "Pinnacle Wealth PMS"

        # Dot-path
        found, val = extract_field_value(stmt, "summary.closing_value")
        assert found is True
        assert val == Decimal("9999.00")

        # Fallback to summary attribute directly
        found, val = extract_field_value(stmt, "closing_value")
        assert found is True
        assert val == Decimal("9999.00")

        # Counts
        found, val = extract_field_value(stmt, "holdings_count")
        assert found is True
        assert val == 2

        # Non-existent attribute
        found, val = extract_field_value(stmt, "non_existent_field")
        assert found is False
        assert val is None


# ---------------------------------------------------------------------------
# Tests: Evaluator Execution and Aggregations
# ---------------------------------------------------------------------------

class TestEvaluatorExecution:
    """Tests for running evaluation cases through the Evaluator."""

    def test_single_case_evaluation_perfect_match(self):
        """Evaluation case with all fields matching returns is_perfect=True and 100% accuracy."""
        stmt = _build_dummy_statement()
        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_pipeline_result(stmt)

        evaluator = Evaluator(pipeline=mock_pipe)
        case = EvaluationCase(
            case_id="case_001",
            file_path="pms_statement.pdf",
            expected_statement_type=StatementType.PMS,
            expected_fields={
                "provider_name": "Pinnacle Wealth PMS",
                "summary.closing_value": Decimal("1473500.00"),
                "summary.closing_cash": Decimal("93500.00"),
                "holdings_count": 2,
            },
        )

        res = evaluator.evaluate_case(case)

        assert isinstance(res, CaseEvaluationResult)
        assert res.case_id == "case_001"
        assert res.processed_successfully is True
        assert res.is_perfect is True
        assert res.total_expected_fields == 4
        assert res.correct_fields == 4
        assert res.incorrect_fields == 0
        assert res.missing_fields == 0
        assert res.field_accuracy == 1.0
        assert res.mismatches == []

    def test_single_case_evaluation_with_discrepancies(self):
        """Mismatched values reflect in incorrect_fields and mismatches list."""
        stmt = _build_dummy_statement(provider_name="Old Provider PMS")
        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_pipeline_result(stmt)

        evaluator = Evaluator(pipeline=mock_pipe)
        case = EvaluationCase(
            case_id="case_002",
            file_path="stmt.pdf",
            expected_fields={
                "provider_name": "Expected Provider PMS",  # Mismatch
                "summary.closing_value": Decimal("1473500.00"),  # Match
                "missing_attr": "Some Value",  # Missing
            },
        )

        res = evaluator.evaluate_case(case)

        assert res.is_perfect is False
        assert res.total_expected_fields == 3
        assert res.correct_fields == 1
        assert res.incorrect_fields == 1
        assert res.missing_fields == 1
        assert res.field_accuracy == pytest.approx(1 / 3)
        assert len(res.mismatches) == 2

    def test_aggregate_metrics_across_multiple_cases(self):
        """Aggregate metrics sum cases, fields, and calculate overall accuracy."""
        stmt_perfect = _build_dummy_statement()
        stmt_discrepant = _build_dummy_statement(provider_name="Wrong PMS")

        def mock_process(path, password=None):
            if "fail" in str(path):
                return _make_pipeline_result(None, status=DocumentStatus.FAILED)
            if "wrong" in str(path):
                return _make_pipeline_result(stmt_discrepant)
            return _make_pipeline_result(stmt_perfect)

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.side_effect = mock_process

        cases = [
            EvaluationCase(
                case_id="c_perfect",
                file_path="perfect.pdf",
                expected_statement_type="PMS",
                expected_fields={"provider_name": "Pinnacle Wealth PMS", "holdings_count": 2},
            ),
            EvaluationCase(
                case_id="c_wrong",
                file_path="wrong.pdf",
                expected_statement_type="PMS",
                expected_fields={"provider_name": "Pinnacle Wealth PMS", "holdings_count": 2},
            ),
            EvaluationCase(
                case_id="c_fail",
                file_path="fail.pdf",
                expected_statement_type="AIF",
                expected_fields={"provider_name": "AIF Fund", "holdings_count": 5},
            ),
        ]

        result = evaluate_cases(cases, pipeline=mock_pipe)

        assert isinstance(result, EvaluationResult)
        assert result.total_cases == 3
        assert result.processed_cases == 3
        assert result.successful_cases == 2
        assert result.failed_cases == 1
        assert result.perfect_cases == 1

        # Field aggregates: 2 + 2 + 2 = 6 total expected fields
        assert result.total_expected_fields == 6
        assert result.correct_fields == 3   # 2 from perfect, 1 from wrong (holdings_count)
        assert result.incorrect_fields == 1 # provider_name from wrong
        assert result.missing_fields == 2   # 2 from fail case
        assert result.overall_field_accuracy == pytest.approx(3 / 6)

        # Performance by statement type
        assert "PMS" in result.by_statement_type
        assert "AIF" in result.by_statement_type
        pms_m = result.by_statement_type["PMS"]
        assert pms_m.total_cases == 2
        assert pms_m.successful_cases == 2
        assert pms_m.perfect_cases == 1
        assert pms_m.field_accuracy == pytest.approx(3 / 4)

        aif_m = result.by_statement_type["AIF"]
        assert aif_m.total_cases == 1
        assert aif_m.successful_cases == 0
        assert aif_m.field_accuracy == 0.0

    def test_pipeline_crash_isolated_without_aborting_run(self):
        """When pipeline raises an unhandled exception, evaluator records failure and continues."""
        stmt = _build_dummy_statement()

        def mock_process(path, password=None):
            if "crash" in str(path):
                raise RuntimeError("Unexpected extraction segmentation fault")
            return _make_pipeline_result(stmt)

        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.side_effect = mock_process

        cases = [
            EvaluationCase(case_id="crash_case", file_path="crash.pdf", expected_fields={"provider_name": "Test"}),
            EvaluationCase(case_id="good_case", file_path="good.pdf", expected_fields={"provider_name": "Pinnacle Wealth PMS"}),
        ]

        result = evaluate_cases(cases, pipeline=mock_pipe)

        assert result.total_cases == 2
        assert result.successful_cases == 1
        assert result.failed_cases == 1
        assert any("segmentation fault" in err for err in result.case_results[0].errors)
        assert result.case_results[1].is_perfect is True

    def test_empty_evaluation_set_handles_zero_division(self):
        """Empty evaluation suite returns cleanly without ZeroDivisionError."""
        evaluator = Evaluator()
        result = evaluator.evaluate_cases([])

        assert result.total_cases == 0
        assert result.processed_cases == 0
        assert result.overall_field_accuracy == 0.0
        assert result.case_results == []

        # Report formatting on empty set
        text_rep = result.to_text_report()
        assert "Total Cases:            0" in text_rep
        assert "Overall Field Accuracy: 0.00%" in text_rep

    def test_deterministic_reproducibility(self):
        """Repeated evaluations of identical inputs produce identical numerical and field outcomes."""
        stmt = _build_dummy_statement()
        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_pipeline_result(stmt)

        cases = [
            EvaluationCase(case_id="c1", file_path="f1.pdf", expected_fields={"provider_name": "Pinnacle Wealth PMS"}),
            EvaluationCase(case_id="c2", file_path="f2.pdf", expected_fields={"holdings_count": 2}),
        ]

        evaluator = Evaluator(pipeline=mock_pipe)
        run1 = evaluator.evaluate_cases(cases)
        run2 = evaluator.evaluate_cases(cases)

        assert run1.overall_field_accuracy == run2.overall_field_accuracy
        assert run1.correct_fields == run2.correct_fields
        assert run1.model_dump() == run2.model_dump()


# ---------------------------------------------------------------------------
# Tests: Reports and Loaders
# ---------------------------------------------------------------------------

class TestReportsAndLoaders:
    """Tests for report serialisation and dataset loaders."""

    def test_json_and_text_report_generation(self, tmp_path: Path):
        """Generates valid JSON reports and human-readable text summaries."""
        stmt = _build_dummy_statement()
        mock_pipe = MagicMock(spec=PipelineInterface)
        mock_pipe.process_file.return_value = _make_pipeline_result(stmt)

        cases = [
            EvaluationCase(
                case_id="case_demo",
                file_path="demo.pdf",
                expected_statement_type="PMS",
                expected_fields={"provider_name": "Pinnacle Wealth PMS"},
            )
        ]

        res = evaluate_cases(cases, pipeline=mock_pipe)

        # Text report
        text_rep = res.to_text_report()
        assert "PMS/AIF PARSER EVALUATION REPORT" in text_rep
        assert "Total Cases:            1" in text_rep
        assert "Overall Field Accuracy: 100.00%" in text_rep

        # JSON report
        json_rep = res.to_json_report()
        parsed_json = json.loads(json_rep)
        assert parsed_json["total_cases"] == 1
        assert parsed_json["overall_field_accuracy"] == 1.0

        # File export
        text_file = tmp_path / "report.txt"
        json_file = tmp_path / "report.json"
        res.write_report(text_file, report_format="text")
        res.write_report(json_file, report_format="json")

        assert text_file.exists()
        assert json_file.exists()
        assert "PMS/AIF PARSER" in text_file.read_text(encoding="utf-8")
        assert json.loads(json_file.read_text(encoding="utf-8"))["total_cases"] == 1

    def test_load_case_from_ground_truth_json(self, tmp_path: Path):
        """Constructs an EvaluationCase from a synthetic ground-truth JSON file."""
        json_file = tmp_path / "format_test.json"
        pdf_file = tmp_path / "format_test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 dummy")

        payload = {
            "label": "format_test",
            "provider_name": "Synthetic Provider",
            "statement_type": "PMS",
            "summary": {
                "closing_value": "500000.00",
                "closing_cash": "25000.00",
            },
            "holdings": [{"security_name": "Sec1"}, {"security_name": "Sec2"}],
            "transactions": [{"type": "buy"}],
        }
        json_file.write_text(json.dumps(payload), encoding="utf-8")

        case = load_case_from_ground_truth(json_file)

        assert case.case_id == "format_test"
        assert case.file_path == str(pdf_file)
        assert case.expected_statement_type == "PMS"
        assert case.expected_fields["provider_name"] == "Synthetic Provider"
        assert case.expected_fields["summary.closing_value"] == "500000.00"
        assert case.expected_fields["holdings_count"] == 2
        assert case.expected_fields["transactions_count"] == 1

    def test_load_cases_from_synthetic_directory(self):
        """Loads evaluation cases from existing synthetic samples directory."""
        samples_dir = Path("samples/synthetic")
        if not samples_dir.exists():
            pytest.skip("samples/synthetic directory not present")

        cases = load_cases_from_directory(samples_dir)
        assert len(cases) >= 4
        assert any(c.case_id == "format_a1" for c in cases)
        assert any(c.case_id == "format_b" for c in cases)
        assert any(c.case_id == "format_c" for c in cases)
