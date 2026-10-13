"""Parser evaluation framework for PMS & AIF financial statements."""
from src.eval.comparator import (
    compare_field_value,
    extract_field_value,
    to_date_str,
    to_decimal,
)
from src.eval.evaluator import (
    Evaluator,
    evaluate_cases,
)
from src.eval.loader import (
    load_case_from_ground_truth,
    load_cases_from_directory,
)
from src.eval.models import (
    CaseEvaluationResult,
    EvaluationCase,
    EvaluationConfig,
    EvaluationResult,
    FieldComparisonResult,
    FieldMatchStatus,
    StatementTypeMetrics,
)
from src.eval.synthetic_specs import build_synthetic_spec_store

__all__ = [
    "Evaluator",
    "evaluate_cases",
    "EvaluationCase",
    "EvaluationConfig",
    "EvaluationResult",
    "CaseEvaluationResult",
    "StatementTypeMetrics",
    "FieldMatchStatus",
    "FieldComparisonResult",
    "compare_field_value",
    "extract_field_value",
    "load_case_from_ground_truth",
    "load_cases_from_directory",
    "to_decimal",
    "to_date_str",
    "build_synthetic_spec_store",
]
