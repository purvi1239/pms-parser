"""Command-line entry point for running parser evaluations: python -m src.eval"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

from src.eval.evaluator import Evaluator
from src.eval.loader import load_cases_from_directory
from src.eval.synthetic_specs import build_synthetic_spec_store
from src.pipeline.models import PipelineConfig
from src.pipeline.pipeline import Pipeline
from src.specs.store import SpecStore


def main(argv: list[str] | None = None) -> int:
    """Run parser evaluation from command line."""
    parser = argparse.ArgumentParser(
        prog="pms-eval",
        description="PMS & AIF Investment Statement Parser Evaluation Framework",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="samples/synthetic",
        help="Directory containing ground-truth JSON files and companion PDFs (default: samples/synthetic).",
    )
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        default=None,
        help="Path to write machine-readable JSON evaluation report.",
    )
    parser.add_argument(
        "--text-out",
        metavar="PATH",
        default=None,
        help="Path to write human-readable text evaluation report.",
    )
    args = parser.parse_args(argv)

    dir_path = Path(args.directory)
    if not dir_path.exists():
        print(f"Error: Directory does not exist: {dir_path}", file=sys.stderr)
        return 1

    try:
        cases = load_cases_from_directory(dir_path)
    except Exception as exc:
        print(f"Error loading evaluation cases: {exc}", file=sys.stderr)
        return 1

    if not cases:
        print(f"Warning: No ground-truth JSON/PDF pairs found in {dir_path}", file=sys.stderr)
        return 0

    # Build synthetic specs in a temporary directory so the production
    # specs/ registry is never modified.
    with tempfile.TemporaryDirectory(prefix="pms_eval_specs_") as tmp_specs_dir:
        spec_store = SpecStore(specs_dir=tmp_specs_dir)
        build_synthetic_spec_store(spec_store, samples_dir=dir_path)

        pipeline = Pipeline(
            spec_store=spec_store,
            config=PipelineConfig(store_results=False),
        )
        evaluator = Evaluator(pipeline=pipeline)
        result = evaluator.evaluate_cases(cases)

    # Print text summary to console
    print(result.to_text_report())

    # Write requested reports
    if args.json_out:
        result.write_report(args.json_out, report_format="json")
        print(f"JSON report written to: {args.json_out}")
    if args.text_out:
        result.write_report(args.text_out, report_format="text")
        print(f"Text report written to: {args.text_out}")

    return 0 if result.failed_cases == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
