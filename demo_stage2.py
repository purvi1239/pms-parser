#!/usr/bin/env python3
"""Stage 2 Demo: Unknown-Format Learning & Spec-Reuse Workflow.

Demonstrates the full learning cycle without any network calls or real credentials:

  5A. Process format_b.pdf with no matching spec → NEEDS_REVIEW
      (unknown format; no learning enabled in standard pipeline mode)
  5B. Extract DocumentIR → run Redactor → send to Learner with a mock LLM
      transport → ParsingSpec generated as draft → PROPOSED
  5C. Inspect redaction summary; explicitly approve the draft by promoting
      it from 'draft_synthetic-format-b' → 'synthetic-format-b' in SpecStore
  5D. "Later run": fresh StatementStore, same SpecStore (now has the approved
      spec with fingerprint) → process format_b.pdf → PARSED

No real LLM is called.  The mock transport is injected as a Callable into
LLMAdapter and returns a deterministic spec JSON derived from the known
format_b layout.  No credentials are exposed or read.

Usage:
    python demo_stage2.py
    python demo_stage2.py --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Production imports (no mock patching of library internals)
# --------------------------------------------------------------------------
from src.eval.synthetic_specs import build_synthetic_spec_store
from src.ir.extractor import extract_pdf
from src.learner.learner import Learner
from src.learner.models import LearnerConfig, ProposalStatus
from src.llm_adapter.adapter import FakeLLMAdapter
from src.models.enums import DocumentStatus, StatementType
from src.pipeline.models import PipelineConfig, PipelineResult
from src.pipeline.pipeline import Pipeline
from src.redactor.redactor import Redactor
from src.specs.store import SpecStore
from src.store.store import StatementStore

# --------------------------------------------------------------------------
# Formatting helpers (reused from demo.py pattern)
# --------------------------------------------------------------------------

_BANNER_WIDTH = 76


def _banner(title: str) -> None:
    print("\n" + "=" * _BANNER_WIDTH)
    print(f"  {title}")
    print("=" * _BANNER_WIDTH)


def _section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, _BANNER_WIDTH - len(title) - 6))


# --------------------------------------------------------------------------
# Mock LLM transport
#
# The LLMAdapter constructor accepts an optional `transport` callable with
# signature:
#     transport(payload: dict[str, Any], config: LLMConfig) -> dict[str, Any]
#
# The return value must be an OpenAI-compatible chat completion dict.
# We return the correct format_b spec JSON deterministically — no network
# call, no API key, no cost.  The spec JSON is taken verbatim from the
# synthetic_specs.py layout so that the learner's own validation loop
# succeeds.
# --------------------------------------------------------------------------

# This is the ParsingSpec JSON the "mock LLM" will return.
# It matches the format_b layout defined in src/eval/synthetic_specs.py.
_FORMAT_B_SPEC_DICT: dict[str, Any] = {
    "format_id": "synthetic-format-b",
    "version": "1.0.0",
    "statement_type": "PMS",
    "description": "Format B: Client Portfolio Report with horizontal summary and Trade Ledger",
    "metadata_fields": {
        "provider_name": {
            "locator_type": "value_matching_pattern",
            "pattern": r"Client Portfolio Report\s*\n([^\n]+)",
        },
        "strategy_name": {
            "locator_type": "value_matching_pattern",
            "pattern": r"Client Portfolio Report\s*\n[^\n]+\n([^\n]+)",
        },
        "client_identifier": {
            "locator_type": "value_right_of_anchor",
            "anchor": "Account Number:",
        },
        "statement_period": {
            "locator_type": "value_right_of_anchor",
            "anchor": "Report Period:",
        },
        "currency": {
            "locator_type": "value_right_of_anchor",
            "anchor": "Base Currency:",
        },
    },
    "summary_fields": {
        "opening_value": {"locator_type": "value_below_anchor", "anchor": "Opening Value"},
        "closing_value": {"locator_type": "value_below_anchor", "anchor": "Closing Value"},
        "opening_cash":  {"locator_type": "value_below_anchor", "anchor": "Opening Cash"},
        "closing_cash":  {"locator_type": "value_below_anchor", "anchor": "Closing Cash"},
        "fees_charged":  {"locator_type": "value_below_anchor", "anchor": "Fees Charged"},
        "income":        {"locator_type": "value_below_anchor", "anchor": "Income"},
        "realized_gains":   {"locator_type": "value_below_anchor", "anchor": "Realised Gains"},
        "unrealized_gains": {"locator_type": "value_below_anchor", "anchor": "Unrealised Gains"},
    },
    "holdings_table": {
        "locator_type": "table_after_anchor",
        "anchor": "Portfolio Holdings",
        "header_keywords": ["Scrip Name", "ISIN Code", "Units", "Purchase Cost", "Current Value"],
        "column_mapping": {
            "security_name": "Scrip Name",
            "isin": "ISIN Code",
            "quantity": "Units",
            "cost": "Purchase Cost",
            "market_value": "Current Value",
            "weight": "Allocation %",
        },
        "multi_page": True,
    },
    "transactions_table": {
        "locator_type": "table_after_anchor",
        "anchor": "Trade Ledger",
        "header_keywords": ["Trade Date", "Activity", "Instrument", "Units", "Consideration"],
        "column_mapping": {
            "date": "Trade Date",
            "type": "Activity",
            "security": "Instrument",
            "quantity": "Units",
            "amount": "Consideration (INR)",
        },
        "multi_page": True,
    },
}


def _build_mock_llm_adapter() -> FakeLLMAdapter:
    """Return a FakeLLMAdapter pre-loaded with the format_b spec as default response.

    FakeLLMAdapter is the project's built-in offline testing adapter:
    it returns a canned in-memory response without any network call,
    API key, or cost.  The spec JSON matches the format_b layout defined
    in src/eval/synthetic_specs.py exactly.
    """
    return FakeLLMAdapter(default_response=_FORMAT_B_SPEC_DICT)


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

def _build_stage2_spec_store(specs_dir: Path, samples_dir: Path) -> SpecStore:
    """Build a SpecStore containing format_a and format_c specs ONLY.

    Format_b is deliberately absent so format_b.pdf looks unknown to the
    pipeline in Scenario 5A.
    """
    spec_store = SpecStore(specs_dir=specs_dir)
    # Register all synthetic specs ...
    build_synthetic_spec_store(spec_store, samples_dir=samples_dir)
    # ... then remove format_b so it is genuinely unknown
    if spec_store.exists("synthetic-format-b"):
        spec_store.delete("synthetic-format-b")
    # Also remove drift spec (not needed for Stage 2 demo)
    if spec_store.exists("synthetic-format-a-drift"):
        spec_store.delete("synthetic-format-a-drift")
    return spec_store


def _make_pipeline(store: StatementStore, spec_store: SpecStore) -> Pipeline:
    """Return a Pipeline with learning disabled (standard production mode)."""
    return Pipeline(
        store=store,
        spec_store=spec_store,
        config=PipelineConfig(enable_learning=False, store_results=True),
    )


# --------------------------------------------------------------------------
# Main demo
# --------------------------------------------------------------------------

def run_demo(verbose: bool = False) -> int:
    project_root = Path(__file__).resolve().parent
    samples_dir = project_root / "samples" / "synthetic"

    if not samples_dir.is_dir():
        print(f"Error: Synthetic samples directory not found at {samples_dir}",
              file=sys.stderr)
        return 1

    _banner("PMS & AIF STATEMENT PARSER — STAGE 2: LEARNING & SPEC-REUSE DEMO")
    print("  Objective  : Unknown format → PII redaction → mock-LLM draft → approval → reuse")
    print("  LLM mode   : Mock transport (no network, no API key, deterministic)")
    print("  Data source: Synthetic fixtures only (no real financial documents)")

    failures: list[str] = []
    summary_rows: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="pms_stage2_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # One shared SpecStore for the whole demo
        spec_store = _build_stage2_spec_store(tmp_path / "specs", samples_dir)

        b_file = samples_dir / "format_b.pdf"
        if not b_file.is_file():
            print(f"Error: format_b.pdf not found at {b_file}", file=sys.stderr)
            return 1

        # ================================================================
        # Scenario 5A: Unknown Format — no matching spec
        # ================================================================
        _banner("SCENARIO 5A: Process format_b.pdf — No Matching Spec")
        print("Objective: Demonstrate that a genuinely unknown statement format is\n"
              "           safely rejected (NEEDS_REVIEW) without auto-learning.")

        store_5a = StatementStore(db_path=":memory:")
        pipeline_5a = _make_pipeline(store_5a, spec_store)

        try:
            res5a: PipelineResult = pipeline_5a.process_file(b_file)

            _section("Scenario 5A — Result")
            print(f"  File           : {b_file.name}")
            print(f"  Status         : {res5a.status.value}")
            print(f"  Format matched : {res5a.format_id or '(none)'}")
            print(f"  Warnings       : {res5a.warnings}")

            summary_rows.append({
                "step": "5A: Unknown format",
                "file": b_file.name,
                "status": res5a.status.value,
                "note": "Expected: NEEDS_REVIEW",
            })

            if res5a.status != DocumentStatus.NEEDS_REVIEW:
                failures.append(
                    f"Scenario 5A: expected NEEDS_REVIEW, got {res5a.status.value}"
                )

        except Exception as exc:
            failures.append(f"Scenario 5A crashed: {exc}")
            print(f"Error: {exc}", file=sys.stderr)

        # ================================================================
        # Scenario 5B: Run Learner with Mock LLM
        # ================================================================
        _banner("SCENARIO 5B: Learner — PII Redaction + Draft Spec Generation")
        print("Objective: Extract DocumentIR → Redact PII → Call mock LLM → Validate spec\n"
              "           → Save draft to SpecStore (PROPOSED, not yet active).")

        try:
            # 1. Extract IR directly (same as what the pipeline would do)
            doc_ir = extract_pdf(b_file)
            full_text = doc_ir.get_all_text()

            # 2. Run Redactor standalone to show what is masked before LLM dispatch
            redactor = Redactor()
            redaction_result = redactor.redact(full_text)

            _section("Scenario 5B — PII Redaction Report")
            print(f"  Original text length : {len(full_text):,} chars")
            print(f"  Redacted text length : {len(redaction_result.redacted_text):,} chars")
            print(f"  PII entities masked  : {len(redaction_result.items)}")
            if redaction_result.items:
                for item in redaction_result.items[:5]:
                    print(f"    [{item.entity_type.value}]  {item.placeholder}")
                if len(redaction_result.items) > 5:
                    print(f"    ... and {len(redaction_result.items) - 5} more")
            else:
                print("    (No PII patterns detected in synthetic document — expected)")
            print(f"  Safe for LLM dispatch: {'YES — no raw PII in context' if not redaction_result.items else 'YES — all PII replaced with placeholders'}")

            # 3. Run Learner — mock transport injected, no network call
            mock_adapter = _build_mock_llm_adapter()
            learner_spec_store = spec_store   # same store — draft will land here
            learner = Learner(
                llm_adapter=mock_adapter,
                spec_store=learner_spec_store,
                config=LearnerConfig(
                    max_repairs=1,
                    auto_activate=False,  # → saves as draft, returns PROPOSED
                    temperature=0.0,
                ),
            )

            _section("Scenario 5B — Mock LLM Call & Spec Validation")
            print("  Dispatching document context to mock LLM transport ...")
            learning_result = learner.learn(
                doc=doc_ir,
                format_id="synthetic-format-b",
                statement_type=StatementType.PMS,
            )

            print(f"  Learning status  : {learning_result.status.value}")
            print(f"  Draft format ID  : {learning_result.format_id}")
            print(f"  Attempts made    : {learning_result.attempts_count}")
            print(f"  Spec passed val  : {learning_result.passed_validation}")

            if learning_result.spec:
                spec = learning_result.spec
                draft_id = f"draft_{spec.format_id}"
                print(f"\n  Draft spec saved to SpecStore as '{draft_id}.json'")
                print(f"  Metadata fields  : {list(spec.metadata_fields.keys())}")
                print(f"  Summary fields   : {list(spec.summary_fields.keys())}")
                print(f"  Holdings table   : {'Yes' if spec.holdings_table else 'No'}")
                print(f"  Transactions     : {'Yes' if spec.transactions_table else 'No'}")
                print(f"  Fingerprint set  : {'Yes' if spec.fingerprint else 'No'}")

            if verbose and learning_result.attempts:
                _section("Scenario 5B — Attempt Detail")
                for att in learning_result.attempts:
                    status = "✓ OK" if att.success else f"✗ {att.errors}"
                    print(f"  Attempt {att.attempt_number}: {status}")

            summary_rows.append({
                "step": "5B: Mock-LLM draft",
                "file": b_file.name,
                "status": learning_result.status.value,
                "note": "Expected: proposed",
            })

            if learning_result.status != ProposalStatus.PROPOSED:
                failures.append(
                    f"Scenario 5B: expected PROPOSED, got {learning_result.status.value}"
                    + (f" — {learning_result.error_message}" if learning_result.error_message else "")
                )
                if learning_result.repair_history and verbose:
                    for err in learning_result.repair_history:
                        print(f"    Repair error: {err}", file=sys.stderr)

        except Exception as exc:
            failures.append(f"Scenario 5B crashed: {exc}")
            print(f"Error in Scenario 5B: {exc}", file=sys.stderr)
            import traceback; traceback.print_exc()

        # ================================================================
        # Scenario 5C: Explicit Approval — promote draft → active spec
        # ================================================================
        _banner("SCENARIO 5C: Explicit Human Approval — Promote Draft to Active Spec")
        print("Objective: A human reviewer (simulated here by the demo) inspects the draft\n"
              "           spec and approves it. The draft is promoted to an active spec\n"
              "           under the canonical format_id (without 'draft_' prefix).\n"
              "           No production spec is silently overwritten without this step.")

        approved_format_id: str | None = None
        try:
            draft_id = "draft_synthetic-format-b"
            target_id = "synthetic-format-b"

            if not spec_store.exists(draft_id):
                raise RuntimeError(
                    f"Draft spec '{draft_id}' not found in SpecStore — "
                    "Scenario 5B must have succeeded for 5C to run."
                )

            draft_spec = spec_store.load(draft_id)

            _section("Scenario 5C — Draft Inspection (pre-approval)")
            print(f"  Draft ID        : {draft_spec.format_id}")
            print(f"  Version         : {draft_spec.version}")
            statement_type_val = (
                draft_spec.statement_type.value
                if hasattr(draft_spec.statement_type, "value")
                else getattr(draft_spec.statement_type, "name", str(draft_spec.statement_type))
            )
            print(f"  Statement type  : {statement_type_val}")
            print(f"  Description     : {draft_spec.description}")
            print(f"  Fingerprint     : {'set (extracted from training document)' if draft_spec.fingerprint else 'absent'}")
            print(f"  Draft metadata  : is_active={draft_spec.metadata.get('is_active')}, "
                  f"status={draft_spec.metadata.get('status')}")

            # --- Promotion: strip draft prefix, clear draft metadata flags ---
            approved_spec = draft_spec.model_copy(deep=True)
            approved_spec.format_id = target_id
            approved_spec.metadata["is_active"] = True
            approved_spec.metadata["status"] = "active"
            approved_spec.metadata["approved_by"] = "demo_stage2 (simulated reviewer)"
            if approved_spec.fingerprint is not None:
                approved_spec.fingerprint = approved_spec.fingerprint.model_copy(
                    update={"format_id": target_id}
                )

            saved_path = spec_store.save(approved_spec, overwrite=True)
            approved_format_id = target_id

            _section("Scenario 5C — Approval Result")
            print(f"  ✓ Draft promoted  : '{draft_id}' → '{target_id}'")
            saved_name = saved_path.name if hasattr(saved_path, "name") else Path(str(saved_path)).name
            print(f"  Saved to          : specs/{saved_name}")
            print(f"  Active in store   : {spec_store.exists(target_id)}")
            print(f"  Fingerprint ready : {approved_spec.fingerprint is not None}")

            summary_rows.append({
                "step": "5C: Approval",
                "file": f"{target_id}.json",
                "status": "APPROVED",
                "note": "Draft promoted to active spec",
            })

        except Exception as exc:
            failures.append(f"Scenario 5C crashed: {exc}")
            print(f"Error in Scenario 5C: {exc}", file=sys.stderr)

        # ================================================================
        # Scenario 5D: "Later Run" — Reprocess using the Saved Spec
        # ================================================================
        _banner("SCENARIO 5D: Later Run — Reprocess format_b.pdf Using Approved Spec")
        print("Objective: A fresh pipeline (new StatementStore, same SpecStore) processes\n"
              "           format_b.pdf.  The approved spec is matched by fingerprint and\n"
              "           the document parses successfully to PARSED.\n"
              "           No LLM call is made in this run.")

        try:
            if approved_format_id is None:
                raise RuntimeError(
                    "Scenario 5C did not complete successfully — cannot run 5D."
                )

            # Fresh StatementStore so format_b.pdf is not seen as a duplicate
            store_5d = StatementStore(db_path=":memory:")
            # Same spec_store (now contains 'synthetic-format-b')
            pipeline_5d = _make_pipeline(store_5d, spec_store)

            res5d: PipelineResult = pipeline_5d.process_file(b_file)

            _section("Scenario 5D — Result")
            print(f"  File           : {b_file.name}")
            print(f"  Status         : {res5d.status.value}")
            print(f"  Format matched : {res5d.format_id or '(none)'}")

            if res5d.statement:
                s = res5d.statement
                print(f"  Provider name  : {s.provider_name or '—'}")
                print(f"  Client ID      : {s.client_identifier or '—'}")
                print(f"  Statement period: {s.statement_period or '—'}")

            h_count = len(res5d.statement.holdings) if (res5d.statement and res5d.statement.holdings) else 0
            t_count = len(res5d.statement.transactions) if (res5d.statement and res5d.statement.transactions) else 0
            print(f"  Holdings       : {h_count} positions")
            print(f"  Transactions   : {t_count} records")

            if res5d.validation_result:
                vr = res5d.validation_result
                print(f"  Validation     : {'PASSED' if vr.passed else 'NEEDS REVIEW'} "
                      f"({len(vr.checks_run)} checks run)")

            if verbose and res5d.warnings:
                print(f"  Warnings       : {res5d.warnings}")

            parsed_ok = res5d.status in (
                DocumentStatus.PARSED,
                DocumentStatus.PARSED_WITH_WARNINGS,
            )
            summary_rows.append({
                "step": "5D: Later run",
                "file": b_file.name,
                "status": res5d.status.value,
                "note": "Expected: PARSED (spec reuse via fingerprint match)",
            })

            if not parsed_ok:
                failures.append(
                    f"Scenario 5D: expected PARSED, got {res5d.status.value}"
                    + (f" — errors: {res5d.errors}" if res5d.errors else "")
                )

        except Exception as exc:
            failures.append(f"Scenario 5D crashed: {exc}")
            print(f"Error in Scenario 5D: {exc}", file=sys.stderr)
            import traceback; traceback.print_exc()

    # ====================================================================
    # Summary Table
    # ====================================================================
    _banner("PMS PARSER — STAGE 2 DEMO SUMMARY")

    headers = ["Step", "File / Spec", "Status", "Notes"]
    col_w = [25, 30, 14, 42]
    row_fmt = "  ".join(f"{{:<{w}}}" for w in col_w)
    sep_bar = "  ".join("─" * w for w in col_w)

    print(row_fmt.format(*headers))
    print(sep_bar)
    for row in summary_rows:
        print(row_fmt.format(
            row["step"], row["file"], row["status"], row["note"]
        ))
    print(sep_bar)

    if failures:
        print(f"\n❌ STAGE 2 DEMO COMPLETED WITH {len(failures)} FAILURE(S):", file=sys.stderr)
        for msg in failures:
            print(f"   • {msg}", file=sys.stderr)
        return 1

    print("\n✓ ALL 4 STAGE 2 SCENARIOS EXECUTED SUCCESSFULLY.")
    print("  Learning, redaction, draft approval, and spec-reuse verified.\n")
    return 0


# --------------------------------------------------------------------------
# CLI entry-point
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="demo_stage2",
        description="PMS Parser Stage 2: learning & spec-reuse demo (mock LLM, no credentials).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Show additional diagnostic details per scenario.",
    )
    args = parser.parse_args()
    return run_demo(verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
