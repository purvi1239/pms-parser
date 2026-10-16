#!/usr/bin/env python3
"""Stage 1 Synthetic Demo Launcher for PMS & AIF Statement Parser.

Runs a deterministic end-to-end demonstration using the synthetic fixtures in
samples/synthetic/:
  1. Known-Format PMS Statement (format_a1.pdf) -> PARSED with extracted holdings,
     transactions, ground-truth reconciliation, and financial validation.
  2. Known-Format AIF Statement (format_c.pdf) -> PARSED with fund valuation,
     NAV per unit consistency check, and cash ledger transactions.
  3. Template Drift Quarantine (format_a_drift.pdf) -> DRIFT_SUSPECTED quarantine
     without automatic learning, protecting production specifications.
  4. Encrypted PDF Handling (format_a1_encrypted.pdf):
     a. Without password -> NEEDS_PASSWORD status with clear CLI instructions.
     b. With candidate password -> Decrypted in memory and PARSED without leaking credentials.

Usage:
  python demo.py
  python demo.py --verbose
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
from typing import Any

from src.cli.cli import _render_single_result, _sep
from src.eval.comparator import compare_field_value, extract_field_value
from src.eval.models import FieldMatchStatus
from src.eval.loader import load_case_from_ground_truth
from src.eval.synthetic_specs import build_synthetic_spec_store
from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineConfig, PipelineResult
from src.pipeline.pipeline import Pipeline
from src.sample_generator.generator import ENCRYPTED_SAMPLE_PASSWORD
from src.specs.store import SpecStore
from src.store.store import StatementStore
from src.validator.models import ValidationFinding, ValidationResult, ValidationSeverity


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_BANNER_WIDTH = 76


def _banner(title: str) -> None:
    print("\n" + "=" * _BANNER_WIDTH)
    print(f"  {title}")
    print("=" * _BANNER_WIDTH)


def _section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, _BANNER_WIDTH - len(title) - 6))


def _format_validation_summary(vr: ValidationResult | None, res: PipelineResult) -> list[str]:
    """Format structured validation checks distinguishing passed, failed, warning, and skipped."""
    if not vr:
        return ["  (No validation results available for this execution state)"]

    lines: list[str] = []
    lines.append(f"  Validation Outcome: {'PASSED' if vr.passed else 'NEEDS REVIEW / FAILED'}")

    findings_by_check: dict[str, list[ValidationFinding]] = {}
    for f in vr.findings:
        findings_by_check.setdefault(f.check, []).append(f)

    # 1. Executed checks
    if vr.checks_run:
        lines.append(f"\n  Executed Checks ({len(vr.checks_run)}):")
        for check_name in vr.checks_run:
            c_key = check_name.replace("check_", "")
            findings = findings_by_check.get(check_name) or findings_by_check.get(c_key) or []
            errors = [f for f in findings if f.severity == ValidationSeverity.ERROR]
            warnings = [f for f in findings if f.severity == ValidationSeverity.WARNING]

            if errors:
                lines.append(f"    ✗ [{check_name}] FAILED ({len(errors)} error{'s' if len(errors) != 1 else ''})")
                for e in errors:
                    lines.append(f"        Message : {e.message}")
                    if e.actual or e.expected:
                        lines.append(f"        Actual  : {e.actual} | Expected: {e.expected}")
            elif warnings:
                lines.append(f"    ⚑ [{check_name}] WARNING ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})")
                for w in warnings:
                    lines.append(f"        Message : {w.message}")
            else:
                lines.append(f"    ✓ [{check_name}] PASSED")

                # Contextual reconciliation detail for key financial checks
                if c_key == "cash_reconciliation" and res.statement and res.statement.summary:
                    s = res.statement.summary
                    if s.opening_cash is not None and s.closing_cash is not None:
                        tx_sum = sum(t.amount for t in res.statement.transactions if t.amount is not None)
                        lines.append(
                            f"        Details : Opening ₹{s.opening_cash:,.2f} + Net Txns ₹{tx_sum:,.2f} "
                            f"= Closing ₹{s.closing_cash:,.2f}"
                        )
                elif c_key == "nav_consistency" and res.statement and res.statement.summary:
                    s = res.statement.summary
                    if s.units is not None and s.nav_per_unit is not None and s.closing_value is not None:
                        computed = s.units * s.nav_per_unit
                        lines.append(
                            f"        Details : {s.units:,.4f} units × ₹{s.nav_per_unit:,.2f} NAV "
                            f"= ₹{computed:,.2f} ≈ Closing Value ₹{s.closing_value:,.2f}"
                        )
                elif c_key == "portfolio_weights" and res.statement and res.statement.holdings:
                    weights = [h.weight for h in res.statement.holdings if h.weight is not None]
                    if weights:
                        lines.append(
                            f"        Details : Weight Sum = {sum(weights):.2f}% across {len(weights)} positions"
                        )
                elif c_key == "quantity_price_value" and res.statement and res.statement.holdings:
                    lines.append(
                        f"        Details : Verified quantity × price consistency against market values"
                    )

    # 2. Skipped checks
    if vr.checks_skipped:
        lines.append(f"\n  Skipped Checks ({len(vr.checks_skipped)}):")
        for check_name in vr.checks_skipped:
            reason = "prerequisite data absent or check not applicable"
            c_lower = check_name.lower()
            if "nav" in c_lower:
                reason = "AIF-specific valuation check; inapplicable to PMS statements"
            elif "weights" in c_lower:
                reason = "no individual equity holdings; AIF statements track fund units"
            elif "quantity" in c_lower:
                reason = "no equity quantity/price values required for this statement type"
            lines.append(f"    ⏭ [{check_name}] SKIPPED — {reason}")

    return lines


def _format_ground_truth_reconciliation(res: PipelineResult, json_path: Path) -> list[str]:
    """Compare extracted statement against ground-truth JSON fixture without hardcoding."""
    if not json_path.is_file():
        return [f"  (Ground truth fixture not found: {json_path.name})"]

    case = load_case_from_ground_truth(json_path)
    lines: list[str] = []
    lines.append(f"  Ground-Truth Fixture: {json_path.name}")

    matched = 0
    total = len(case.expected_fields)
    mismatches: list[str] = []

    for field_name, expected_val in case.expected_fields.items():
        found, actual_val = extract_field_value(res.statement, field_name)
        cmp_res = compare_field_value(
            field_name=field_name,
            expected=expected_val,
            actual=actual_val,
            is_extracted=found,
        )
        if cmp_res.status == FieldMatchStatus.CORRECT:
            matched += 1
        else:
            mismatches.append(f"    • {field_name}: expected={expected_val!r}, actual={actual_val!r}")

    accuracy = (matched / total * 100.0) if total > 0 else 0.0
    lines.append(f"  Reconciliation Match: {matched}/{total} fields match ground truth ({accuracy:.1f}%)")
    if mismatches:
        lines.append("  Field Discrepancies:")
        lines.extend(mismatches)
    else:
        lines.append("  ✓ All evaluated fields match ground-truth expectations exactly.")

    return lines


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------

def _build_spec_store(specs_dir: Path, samples_dir: Path) -> SpecStore:
    """Build an isolated SpecStore with all synthetic specs except drift.

    Drift spec is excluded so format_a_drift.pdf is matched against the baseline
    Format A spec, properly triggering DRIFT_SUSPECTED.
    """
    spec_store = SpecStore(specs_dir=specs_dir)
    build_synthetic_spec_store(spec_store, samples_dir=samples_dir)
    if spec_store.exists("synthetic-format-a-drift"):
        spec_store.delete("synthetic-format-a-drift")
    return spec_store


def _setup_isolated_pipeline(
    store: StatementStore,
    spec_store: SpecStore,
) -> Pipeline:
    """Create a Pipeline backed by the supplied store and spec_store."""
    return Pipeline(
        store=store,
        spec_store=spec_store,
        config=PipelineConfig(
            enable_learning=False,
            store_results=True,
        ),
    )


# ---------------------------------------------------------------------------
# Demo execution
# ---------------------------------------------------------------------------

def run_demo(verbose: bool = False) -> int:
    """Run all Stage 1 synthetic demo scenarios and return exit code 0 on success."""
    project_root = Path(__file__).resolve().parent
    samples_dir = project_root / "samples" / "synthetic"

    if not samples_dir.is_dir() or not (samples_dir / "format_a1.pdf").is_file():
        print(f"  Notice: Synthetic samples not found at {samples_dir}. Generating fixtures...", flush=True)
        from src.sample_generator.generator import generate_all
        generate_all(samples_dir)

    _banner("PMS & AIF STATEMENT PARSER — STAGE 1 SYNTHETIC DEMO")
    print("  Repository root :", project_root)
    print("  Samples source  :", samples_dir)
    print("  Execution mode  : Isolated in-memory pipeline (deterministic)")

    demo_results: list[dict[str, Any]] = []
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="pms_demo_specs_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Shared spec_store used by all scenarios (specs never written to production specs/)
        spec_store = _build_spec_store(tmp_path / "specs", samples_dir)

        # Main pipeline + in-memory store: shared for scenarios 1, 2, 3, 4A
        main_store = StatementStore(db_path=":memory:")
        pipeline = _setup_isolated_pipeline(main_store, spec_store)

        # ===================================================================
        # Scenario 1: Known-Format PMS Statement (format_a1.pdf)
        # ===================================================================
        _banner("SCENARIO 1: Known-Format PMS Statement (format_a1.pdf)")
        print("Objective: Demonstrate layout recognition, structured extraction, ground-truth")
        print("           reconciliation, and financial validation on a standard PMS statement.")

        s1_file = samples_dir / "format_a1.pdf"
        s1_json = samples_dir / "format_a1.json"

        try:
            res1 = pipeline.process_file(s1_file)
            print("\n".join(_render_single_result(res1, s1_file, verbose=verbose)))

            _section("Scenario 1 — Financial Validation Details")
            print("\n".join(_format_validation_summary(res1.validation_result, res1)))

            _section("Scenario 1 — Ground-Truth Verification")
            print("\n".join(_format_ground_truth_reconciliation(res1, s1_json)))

            h_count = len(res1.statement.holdings) if (res1.statement and res1.statement.holdings) else 0
            t_count = len(res1.statement.transactions) if (res1.statement and res1.statement.transactions) else 0
            if res1.validation_result:
                val_status = "Passed" if res1.validation_result.passed else "See Validation"
            else:
                val_status = "N/A (Not run)"

            demo_results.append({
                "scenario": "1. Known PMS",
                "file": s1_file.name,
                "status": res1.status.value,
                "format": res1.format_id or "—",
                "holdings": f"{h_count} positions",
                "transactions": f"{t_count} txns",
                "validation": val_status,
                "quarantined": "No",
            })

            if res1.status not in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS):
                failures.append(f"Scenario 1 failed: expected PARSED, got {res1.status.value}")

        except Exception as exc:
            failures.append(f"Scenario 1 crashed: {exc}")
            print(f"Error in Scenario 1: {exc}", file=sys.stderr)

        # ===================================================================
        # Scenario 2: Known-Format AIF Statement (format_c.pdf)
        # ===================================================================
        _banner("SCENARIO 2: Known-Format AIF Statement (format_c.pdf)")
        print("Objective: Demonstrate AIF Category II statement parsing, fund valuation summary,")
        print("           cash ledger transactions, and NAV per unit reconciliation.")

        s2_file = samples_dir / "format_c.pdf"
        s2_json = samples_dir / "format_c.json"

        try:
            res2 = pipeline.process_file(s2_file)
            print("\n".join(_render_single_result(res2, s2_file, verbose=verbose)))

            _section("Scenario 2 — Financial Validation & NAV Consistency")
            print("\n".join(_format_validation_summary(res2.validation_result, res2)))

            _section("Scenario 2 — Ground-Truth Verification")
            print("\n".join(_format_ground_truth_reconciliation(res2, s2_json)))

            h_count2 = len(res2.statement.holdings) if (res2.statement and res2.statement.holdings) else 0
            t_count2 = len(res2.statement.transactions) if (res2.statement and res2.statement.transactions) else 0
            if res2.validation_result:
                val_status2 = "Passed (NAV Verified)" if res2.validation_result.passed else "See Validation"
            else:
                val_status2 = "N/A (Not run)"

            demo_results.append({
                "scenario": "2. Known AIF",
                "file": s2_file.name,
                "status": res2.status.value,
                "format": res2.format_id or "—",
                "holdings": f"{h_count2} positions",
                "transactions": f"{t_count2} txns",
                "validation": val_status2,
                "quarantined": "No",
            })

            if res2.status not in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS):
                failures.append(f"Scenario 2 failed: expected PARSED, got {res2.status.value}")

        except Exception as exc:
            failures.append(f"Scenario 2 crashed: {exc}")
            print(f"Error in Scenario 2: {exc}", file=sys.stderr)

        # ===================================================================
        # Scenario 3: Template Drift Detection & Quarantine (format_a_drift.pdf)
        # ===================================================================
        _banner("SCENARIO 3: Template Drift Detection & Quarantine (format_a_drift.pdf)")
        print("Objective: Exercise layout drift detection. A mutated statement matching near-match")
        print("           threshold must trigger DRIFT_SUSPECTED and be quarantined without auto-learning.")

        s3_file = samples_dir / "format_a_drift.pdf"

        try:
            res3 = pipeline.process_file(s3_file)
            print("\n".join(_render_single_result(res3, s3_file, verbose=verbose)))

            _section("Scenario 3 — Drift Safeguard Inspection")
            sim_score = res3.metadata.get("near_match_score")
            print(f"  Fingerprint Similarity : {sim_score:.4f}" if sim_score is not None else "  Fingerprint matched in drift band")
            print(f"  Resulting Status       : {res3.status.value}")
            print(f"  Quarantine Flag        : {res3.is_quarantined} (Held for human review)")
            print(f"  Learner Invocation     : None (LLM is not called automatically on drift)")
            print(f"  Active Spec Store      : Unaltered (production specs protected)")

            demo_results.append({
                "scenario": "3. Layout Drift",
                "file": s3_file.name,
                "status": res3.status.value,
                "format": res3.format_id or "synthetic-format-a",
                "holdings": "—",
                "transactions": "—",
                "validation": "N/A (Quarantined)",
                "quarantined": "Yes (Quarantined)",
            })

            if res3.status != DocumentStatus.DRIFT_SUSPECTED:
                failures.append(f"Scenario 3 failed: expected DRIFT_SUSPECTED, got {res3.status.value}")

        except Exception as exc:
            failures.append(f"Scenario 3 crashed: {exc}")
            print(f"Error in Scenario 3: {exc}", file=sys.stderr)

        # ===================================================================
        # Scenario 4A: Encrypted PDF without Password (format_a1_encrypted.pdf)
        # ===================================================================
        _banner("SCENARIO 4A: Encrypted PDF without Password (format_a1_encrypted.pdf)")
        print("Objective: Demonstrate encrypted PDF detection when no password is provided.")
        print("           Document must yield NEEDS_PASSWORD with clear instructions.")

        s4_file = samples_dir / "format_a1_encrypted.pdf"

        try:
            res4a = pipeline.process_file(s4_file)
            print("\n".join(_render_single_result(res4a, s4_file, verbose=verbose)))

            _section("Scenario 4A — Password Requirement Diagnostic")
            print(f"  Resulting Status       : {res4a.status.value}")
            # Show error summary without leaking absolute paths
            errs = [e.replace(str(project_root) + "/", "") for e in res4a.errors]
            print(f"  Pipeline Errors        : {errs}")
            print( "  CLI Resolution Guide   : Pass candidate password via '--password PASSWORD' option:")
            print( "                           python -m src.cli samples/synthetic/format_a1_encrypted.pdf --password <PASSWORD>")

            demo_results.append({
                "scenario": "4a. Encrypted (No Pwd)",
                "file": s4_file.name,
                "status": res4a.status.value,
                "format": "—",
                "holdings": "—",
                "transactions": "—",
                "validation": "N/A (Needs Pwd)",
                "quarantined": "Yes (Needs Password)",
            })

            if res4a.status != DocumentStatus.NEEDS_PASSWORD:
                failures.append(f"Scenario 4a failed: expected NEEDS_PASSWORD, got {res4a.status.value}")

        except Exception as exc:
            failures.append(f"Scenario 4a crashed: {exc}")
            print(f"Error in Scenario 4a: {exc}", file=sys.stderr)

        # ===================================================================
        # Scenario 4B: Encrypted PDF with Known Candidate Password
        # NOTE: Uses a *separate* in-memory store so the encrypted file hash
        #       is not already seen as a duplicate from Scenario 4A's attempt.
        #       This isolation is demo-only; production duplicate detection is
        #       not changed.
        # ===================================================================
        _banner("SCENARIO 4B: Encrypted PDF with Candidate Password (format_a1_encrypted.pdf)")
        print("Objective: Provide known candidate password securely in memory. PDF must decrypt,")
        print("           parse successfully, and complete validation without leaking the password.")

        try:
            # Fresh isolated store so the NEEDS_PASSWORD hash record from 4A
            # cannot trigger a false DUPLICATE result here.
            enc_store = StatementStore(db_path=":memory:")
            enc_pipeline = _setup_isolated_pipeline(enc_store, spec_store)

            res4b = enc_pipeline.process_file(s4_file, password=ENCRYPTED_SAMPLE_PASSWORD)
            print("\n".join(_render_single_result(res4b, s4_file, verbose=verbose)))

            _section("Scenario 4B — Decryption & Security Verification")
            print("  Credential Handling    : In-memory candidate passed securely; never logged or echoed")
            print(f"  Decrypted Status       : {res4b.status.value}")
            print(f"  Matched Format ID      : {res4b.format_id}")

            _section("Scenario 4B — Financial Validation")
            print("\n".join(_format_validation_summary(res4b.validation_result, res4b)))

            parsed_ok = res4b.status in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS)
            h_count4b = len(res4b.statement.holdings) if (res4b.statement and res4b.statement.holdings) else 0
            t_count4b = len(res4b.statement.transactions) if (res4b.statement and res4b.statement.transactions) else 0
            if parsed_ok and res4b.validation_result:
                val_status4b = "Passed" if res4b.validation_result.passed else "See Validation"
            elif not parsed_ok:
                val_status4b = "N/A (Not Parsed)"
            else:
                val_status4b = "N/A (Not run)"

            demo_results.append({
                "scenario": "4b. Encrypted (With Pwd)",
                "file": s4_file.name,
                "status": res4b.status.value,
                "format": res4b.format_id or "—",
                "holdings": f"{h_count4b} positions" if parsed_ok else "—",
                "transactions": f"{t_count4b} txns" if parsed_ok else "—",
                "validation": val_status4b,
                "quarantined": "No",
            })

            if not parsed_ok:
                failures.append(f"Scenario 4b failed: expected PARSED, got {res4b.status.value}")

        except Exception as exc:
            failures.append(f"Scenario 4b crashed: {exc}")
            print(f"Error in Scenario 4b: {exc}", file=sys.stderr)

    # =======================================================================
    # Final Demo Summary Table
    # =======================================================================
    _banner("PMS PARSER — STAGE 1 SYNTHETIC DEMO SUMMARY")

    headers = ["# Scenario", "Fixture File", "Format ID", "Status", "Holdings", "Txns", "Validation", "Quarantined"]
    col_w = [25, 25, 23, 17, 13, 8, 23, 21]

    row_fmt = "  ".join(f"{{:<{w}}}" for w in col_w)
    sep_bar = "  ".join("─" * w for w in col_w)

    print(row_fmt.format(*headers))
    print(sep_bar)

    for item in demo_results:
        print(row_fmt.format(
            item["scenario"],
            item["file"],
            item["format"],
            item["status"],
            item["holdings"],
            item["transactions"],
            item["validation"],
            item["quarantined"],
        ))

    print(sep_bar)

    if failures:
        print(f"\n❌ DEMO COMPLETED WITH {len(failures)} FAILURE(S):", file=sys.stderr)
        for fail_msg in failures:
            print(f"   • {fail_msg}", file=sys.stderr)
        return 1

    print("\n✓ ALL 5 DEMO SCENARIOS EXECUTED SUCCESSFULLY AS SPECIFIED.")
    print("  Stage 1 (Synthetic Ingestion, Validation, Drift & Encryption) verified.\n")
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="demo",
        description="Run PMS & AIF Statement Parser Stage 1 synthetic demo.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Display extended diagnostic details for each parsed document.",
    )
    args = parser.parse_args()
    return run_demo(verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
