"""Loaders for evaluation cases from ground-truth JSON files and sample directories."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.eval.models import EvaluationCase
from src.sample_generator.generator import ENCRYPTED_SAMPLE_LABEL, ENCRYPTED_SAMPLE_PASSWORD


def load_case_from_ground_truth(
    json_path: Path | str,
    pdf_path: Path | str | None = None,
    password: str | None = None,
) -> EvaluationCase:
    """Construct an EvaluationCase from a ground-truth JSON file and its companion PDF.

    Args:
        json_path: Path to ground-truth JSON file.
        pdf_path: Path to companion PDF. If None, resolves to companion file with .pdf extension.
        password: Optional explicit password for encrypted evaluation PDFs.

    Returns:
        Structured EvaluationCase ready for evaluation execution.
    """
    jpath = Path(json_path)
    if not jpath.exists():
        raise FileNotFoundError(f"Ground-truth JSON file does not exist: {jpath}")

    data = json.loads(jpath.read_text(encoding="utf-8"))

    if pdf_path is None:
        ppath = jpath.with_suffix(".pdf")
    else:
        ppath = Path(pdf_path)

    case_id = data.get("label", jpath.stem)
    statement_type = data.get("statement_type")

    # Resolve password for known encrypted test samples
    pwd = password
    if pwd is None and case_id == ENCRYPTED_SAMPLE_LABEL:
        pwd = ENCRYPTED_SAMPLE_PASSWORD

    # Build flattened expected fields dictionary
    expected_fields: dict[str, Any] = {}

    # 1. Top-level statement scalar fields
    scalar_keys = [
        "provider_name",
        "strategy_name",
        "client_identifier",
        "statement_period",
        "currency",
        "statement_type",
    ]
    for k in scalar_keys:
        if k in data and data[k] is not None:
            expected_fields[k] = data[k]

    # 2. Portfolio valuation summary fields
    if "summary" in data and isinstance(data["summary"], dict):
        summary_dict = data["summary"]
        for sk, sv in summary_dict.items():
            if sv is not None:
                expected_fields[f"summary.{sk}"] = sv

    # 3. Structural counts
    if "holdings" in data and isinstance(data["holdings"], list):
        expected_fields["holdings_count"] = len(data["holdings"])

    if "transactions" in data and isinstance(data["transactions"], list):
        expected_fields["transactions_count"] = len(data["transactions"])

    return EvaluationCase(
        case_id=case_id,
        file_path=str(ppath),
        expected_statement_type=statement_type,
        expected_fields=expected_fields,
        password=pwd,
        metadata={"source_json": str(jpath)},
    )


def load_cases_from_directory(directory: Path | str) -> list[EvaluationCase]:
    """Scan directory and load evaluation cases for all ground-truth JSON/PDF pairs.

    Discovers all *.json files with matching *.pdf files, sorted deterministically by filename.

    Args:
        directory: Directory containing synthetic samples or ground-truth pairs.

    Returns:
        Sorted list of EvaluationCase objects.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory does not exist: {dir_path}")
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {dir_path}")

    cases: list[EvaluationCase] = []
    # Find all JSON files and sort deterministically
    json_files = sorted(dir_path.glob("*.json"), key=lambda p: (p.name, str(p)))

    for jf in json_files:
        companion_pdf = jf.with_suffix(".pdf")
        if companion_pdf.is_file():
            cases.append(load_case_from_ground_truth(jf, companion_pdf))

    return cases
