"""Evaluation orchestrator coordinating pipeline execution and metric aggregation."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from src.eval.comparator import compare_field_value, extract_field_value
from src.eval.models import (
    CaseEvaluationResult,
    EvaluationCase,
    EvaluationConfig,
    EvaluationResult,
    FieldComparisonResult,
    FieldMatchStatus,
    StatementTypeMetrics,
)
from src.ingestion.folder import PipelineInterface
from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineConfig, PipelineResult
from src.pipeline.pipeline import Pipeline


class Evaluator:
    """Orchestrates parsing of evaluation cases and calculates precision metrics."""

    def __init__(
        self,
        pipeline: Pipeline | PipelineInterface | None = None,
        config: EvaluationConfig | None = None,
    ) -> None:
        """Initialize Evaluator with an optional pipeline and evaluation configuration.

        Args:
            pipeline: Pipeline or PipelineInterface. If None, instantiates a default
                      Pipeline configured with store_results=False to avoid polluting DB.
            config: Optional evaluation settings (tolerances, normalizations).
        """
        self.config = config or EvaluationConfig()
        if pipeline is None:
            # Default to isolated in-memory pipeline without database persistence
            self.pipeline = Pipeline(config=PipelineConfig(store_results=False))
        else:
            self.pipeline = pipeline

    def evaluate_case(self, case: EvaluationCase) -> CaseEvaluationResult:
        """Evaluate a single test document against its expected outputs.

        Args:
            case: EvaluationCase defining input document and ground truth.

        Returns:
            CaseEvaluationResult with field matches and performance stats.
        """
        file_path_obj = Path(case.file_path)
        errors: list[str] = []
        pipeline_status: str | None = None
        processed_successfully = False
        res: PipelineResult | None = None

        # 1. Execute document parsing through Pipeline
        try:
            res = self.pipeline.process_file(file_path_obj, password=case.password)
            pipeline_status = res.status.value
            processed_successfully = res.status in (
                DocumentStatus.PARSED,
                DocumentStatus.PARSED_WITH_WARNINGS,
            )
            if res.errors:
                errors.extend(res.errors)
        except Exception as exc:
            errors.append(f"Pipeline execution crashed: {exc}")
            pipeline_status = DocumentStatus.FAILED.value
            processed_successfully = False

        statement = res.statement if res else None

        # Determine resolved statement type
        stype = case.expected_statement_type
        if statement and statement.statement_type:
            stype = statement.statement_type.value

        # 2. Field-level comparisons
        field_results: list[FieldComparisonResult] = []
        correct_count = 0
        incorrect_count = 0
        missing_count = 0
        unexpected_count = 0

        # Evaluate all expected fields
        for field_name, expected_val in case.expected_fields.items():
            found, actual_val = extract_field_value(statement, field_name)
            cmp_res = compare_field_value(
                field_name=field_name,
                expected=expected_val,
                actual=actual_val,
                is_extracted=found,
                config=self.config,
            )
            field_results.append(cmp_res)

            if cmp_res.status == FieldMatchStatus.CORRECT:
                correct_count += 1
            elif cmp_res.status == FieldMatchStatus.INCORRECT:
                incorrect_count += 1
            elif cmp_res.status == FieldMatchStatus.MISSING:
                missing_count += 1
            elif cmp_res.status == FieldMatchStatus.UNEXPECTED:
                unexpected_count += 1

        total_expected = len(case.expected_fields)
        accuracy = (correct_count / total_expected) if total_expected > 0 else 0.0
        is_perfect = (
            processed_successfully
            and total_expected > 0
            and correct_count == total_expected
            and incorrect_count == 0
            and missing_count == 0
        )

        return CaseEvaluationResult(
            case_id=case.case_id,
            file_path=case.file_path,
            statement_type=str(stype) if stype else None,
            pipeline_status=pipeline_status,
            processed_successfully=processed_successfully,
            is_perfect=is_perfect,
            total_expected_fields=total_expected,
            correct_fields=correct_count,
            incorrect_fields=incorrect_count,
            missing_fields=missing_count,
            unexpected_fields=unexpected_count,
            field_accuracy=accuracy,
            field_results=field_results,
            errors=errors,
        )

    def evaluate_cases(self, cases: Sequence[EvaluationCase]) -> EvaluationResult:
        """Run an evaluation batch across multiple test cases and aggregate metrics.

        Args:
            cases: Sequence of evaluation cases to process.

        Returns:
            EvaluationResult summarizing overall accuracy, breakdown by type, and mismatches.
        """
        overall_result = EvaluationResult(total_cases=len(cases))

        # Handle empty set gracefully without division by zero
        if not cases:
            return overall_result

        # Grouping for statement type breakdown
        type_buckets: dict[str, list[CaseEvaluationResult]] = {}

        for case in cases:
            case_res = self.evaluate_case(case)
            overall_result.case_results.append(case_res)
            overall_result.processed_cases += 1

            if case_res.processed_successfully:
                overall_result.successful_cases += 1
            else:
                overall_result.failed_cases += 1

            if case_res.is_perfect:
                overall_result.perfect_cases += 1

            overall_result.total_expected_fields += case_res.total_expected_fields
            overall_result.correct_fields += case_res.correct_fields
            overall_result.incorrect_fields += case_res.incorrect_fields
            overall_result.missing_fields += case_res.missing_fields
            overall_result.unexpected_fields += case_res.unexpected_fields

            # Grouping by statement type
            stype = case_res.statement_type or "UNKNOWN"
            if stype not in type_buckets:
                type_buckets[stype] = []
            type_buckets[stype].append(case_res)

        # Calculate overall accuracy
        if overall_result.total_expected_fields > 0:
            overall_result.overall_field_accuracy = (
                overall_result.correct_fields / overall_result.total_expected_fields
            )
        else:
            overall_result.overall_field_accuracy = 0.0

        # Calculate breakdown metrics by statement type
        for stype, bucket in type_buckets.items():
            tot_exp = sum(c.total_expected_fields for c in bucket)
            corr = sum(c.correct_fields for c in bucket)
            incorr = sum(c.incorrect_fields for c in bucket)
            miss = sum(c.missing_fields for c in bucket)
            succ = sum(1 for c in bucket if c.processed_successfully)
            perf = sum(1 for c in bucket if c.is_perfect)

            overall_result.by_statement_type[stype] = StatementTypeMetrics(
                statement_type=stype,
                total_cases=len(bucket),
                successful_cases=succ,
                perfect_cases=perf,
                total_expected_fields=tot_exp,
                correct_fields=corr,
                incorrect_fields=incorr,
                missing_fields=miss,
                field_accuracy=(corr / tot_exp) if tot_exp > 0 else 0.0,
            )

        return overall_result


def evaluate_cases(
    cases: Sequence[EvaluationCase],
    pipeline: Pipeline | PipelineInterface | None = None,
    config: EvaluationConfig | None = None,
) -> EvaluationResult:
    """Convenience function to evaluate a sequence of cases and return an EvaluationResult."""
    evaluator = Evaluator(pipeline=pipeline, config=config)
    return evaluator.evaluate_cases(cases)
