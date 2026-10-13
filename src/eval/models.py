"""Data models for parser evaluation framework."""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DocumentStatus, StatementType


class FieldMatchStatus(str, Enum):
    """Outcome of comparing an actual parsed field value against expected value."""

    CORRECT = "CORRECT"
    """Actual value matches expected value within tolerance."""

    INCORRECT = "INCORRECT"
    """Actual value is present but differs from expected value."""

    MISSING = "MISSING"
    """Expected field value was not extracted or is None in actual output."""

    UNEXPECTED = "UNEXPECTED"
    """Field was not expected but present in actual output (when checking unexpected fields)."""


class FieldComparisonResult(BaseModel):
    """Detailed outcome of a single field comparison."""

    model_config = ConfigDict(validate_assignment=True)

    field_name: str
    """Name or dot-path of the field (e.g. 'provider_name', 'summary.closing_value')."""

    expected: Any = None
    """Expected ground-truth value."""

    actual: Any = None
    """Actual value extracted by the parser."""

    status: FieldMatchStatus
    """Evaluation status for this field."""

    difference: str | None = None
    """Human-readable discrepancy explanation if values mismatch."""

    message: str | None = None
    """Additional diagnostic context."""


class EvaluationCase(BaseModel):
    """Specification of an evaluation test document and expected outputs."""

    model_config = ConfigDict(validate_assignment=True)

    case_id: str
    """Stable unique identifier for the evaluation case."""

    file_path: str
    """Path to the input statement PDF file."""

    expected_statement_type: StatementType | str | None = None
    """Expected statement category (e.g. PMS or AIF)."""

    expected_fields: dict[str, Any] = Field(default_factory=dict)
    """Dictionary of expected field names and values to evaluate."""

    password: str | None = None
    """Password for encrypted evaluation documents."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Optional metadata describing scenario, origin, or test tags."""


class CaseEvaluationResult(BaseModel):
    """Evaluation findings and field match results for a single evaluation case."""

    model_config = ConfigDict(validate_assignment=True)

    case_id: str
    """Evaluation case identifier."""

    file_path: str
    """Source document file path."""

    statement_type: str | None = None
    """Identified or expected statement type."""

    pipeline_status: str | None = None
    """Final pipeline lifecycle status (e.g. PARSED, FAILED)."""

    processed_successfully: bool = False
    """True if document was parsed without fatal errors."""

    is_perfect: bool = False
    """True if processed successfully and 100% of expected fields matched."""

    total_expected_fields: int = 0
    """Total number of expected fields evaluated for this case."""

    correct_fields: int = 0
    """Count of successfully matched fields."""

    incorrect_fields: int = 0
    """Count of mismatched fields."""

    missing_fields: int = 0
    """Count of missing expected fields."""

    unexpected_fields: int = 0
    """Count of unexpected fields."""

    field_accuracy: float = 0.0
    """Case accuracy: correct_fields / total_expected_fields (0.0 if empty)."""

    field_results: list[FieldComparisonResult] = Field(default_factory=list)
    """Per-field comparison results."""

    errors: list[str] = Field(default_factory=list)
    """Processing or pipeline execution error messages."""

    @property
    def mismatches(self) -> list[FieldComparisonResult]:
        """Return list of fields that were incorrect, missing, or unexpected."""
        return [r for r in self.field_results if r.status != FieldMatchStatus.CORRECT]


class StatementTypeMetrics(BaseModel):
    """Aggregated evaluation metrics grouped by statement type (PMS, AIF)."""

    model_config = ConfigDict(validate_assignment=True)

    statement_type: str
    total_cases: int = 0
    successful_cases: int = 0
    perfect_cases: int = 0
    total_expected_fields: int = 0
    correct_fields: int = 0
    incorrect_fields: int = 0
    missing_fields: int = 0
    field_accuracy: float = 0.0


class EvaluationConfig(BaseModel):
    """Operational parameters for evaluation runs."""

    model_config = ConfigDict(validate_assignment=True)

    monetary_tolerance: Decimal = Decimal("0.01")
    """Maximum allowable absolute numeric discrepancy for Decimal amounts."""

    date_format_normalization: bool = True
    """Whether to normalize dates to ISO YYYY-MM-DD for comparison."""

    check_unexpected_fields: bool = False
    """Whether to flag fields present in actual statement but omitted in expected dict."""


class EvaluationResult(BaseModel):
    """Comprehensive summary of evaluation performance across all cases."""

    model_config = ConfigDict(validate_assignment=True)

    total_cases: int = 0
    """Total number of evaluation cases evaluated."""

    processed_cases: int = 0
    """Count of cases processed through the pipeline."""

    successful_cases: int = 0
    """Count of cases successfully parsed (PARSED or PARSED_WITH_WARNINGS)."""

    failed_cases: int = 0
    """Count of cases that failed pipeline execution or extraction."""

    perfect_cases: int = 0
    """Count of cases where all evaluated fields matched expected values."""

    total_expected_fields: int = 0
    """Sum of expected fields across all evaluated cases."""

    correct_fields: int = 0
    """Total fields correctly extracted."""

    incorrect_fields: int = 0
    """Total fields with value discrepancies."""

    missing_fields: int = 0
    """Total expected fields absent from actual output."""

    unexpected_fields: int = 0
    """Total unexpected fields flagged."""

    overall_field_accuracy: float = 0.0
    """Overall field-level accuracy: correct_fields / total_expected_fields."""

    by_statement_type: dict[str, StatementTypeMetrics] = Field(default_factory=dict)
    """Breakdown of metrics by statement type (PMS, AIF)."""

    case_results: list[CaseEvaluationResult] = Field(default_factory=list)
    """Detailed per-case results."""

    errors: list[str] = Field(default_factory=list)
    """Execution errors encountered during evaluation."""

    def to_text_report(self) -> str:
        """Generate a human-readable text report."""
        lines = [
            "==================================================",
            "        PMS/AIF PARSER EVALUATION REPORT         ",
            "==================================================",
            f"Total Cases:            {self.total_cases}",
            f"Processed Cases:        {self.processed_cases}",
            f"Successful Cases:       {self.successful_cases}",
            f"Failed Cases:           {self.failed_cases}",
            f"Perfect Cases:          {self.perfect_cases} ({self._pct(self.perfect_cases, self.total_cases)})",
            "--------------------------------------------------",
            f"Total Expected Fields:  {self.total_expected_fields}",
            f"Correct Fields:         {self.correct_fields}",
            f"Incorrect Fields:       {self.incorrect_fields}",
            f"Missing Fields:         {self.missing_fields}",
            f"Overall Field Accuracy: {self.overall_field_accuracy * 100:.2f}%",
            "--------------------------------------------------",
        ]

        if self.by_statement_type:
            lines.append("PERFORMANCE BY STATEMENT TYPE:")
            for stype, m in sorted(self.by_statement_type.items()):
                lines.append(
                    f"  [{stype}] Cases: {m.total_cases} | "
                    f"Success: {m.successful_cases}/{m.total_cases} | "
                    f"Perfect: {m.perfect_cases} | "
                    f"Accuracy: {m.field_accuracy * 100:.2f}%"
                )
            lines.append("--------------------------------------------------")

        # Discrepancies summary
        mismatched_cases = [c for c in self.case_results if not c.is_perfect]
        if mismatched_cases:
            lines.append(f"CASES WITH DISCREPANCIES ({len(mismatched_cases)}):")
            for c in mismatched_cases:
                lines.append(f"  * Case: {c.case_id} ({Path(c.file_path).name}) [Status: {c.pipeline_status}]")
                for m in c.mismatches:
                    lines.append(f"      - {m.field_name}: expected={m.expected!r}, actual={m.actual!r} ({m.status.value})")
                if c.errors:
                    for err in c.errors:
                        lines.append(f"      - ERROR: {err}")
        else:
            lines.append("ALL EVALUATED CASES MATCHED 100% PERFECTLY!")

        lines.append("==================================================")
        return "\n".join(lines)

    def to_json_report(self, indent: int = 2) -> str:
        """Generate a machine-readable JSON report."""
        return self.model_dump_json(indent=indent)

    def write_report(self, output_path: Path | str, report_format: str = "text") -> None:
        """Write evaluation report to the specified file path."""
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if report_format.lower() == "json":
            out_p.write_text(self.to_json_report(), encoding="utf-8")
        else:
            out_p.write_text(self.to_text_report(), encoding="utf-8")

    @staticmethod
    def _pct(part: int, whole: int) -> str:
        if whole == 0:
            return "0.0%"
        return f"{part / whole * 100:.1f}%"
