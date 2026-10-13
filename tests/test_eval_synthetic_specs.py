"""Tests for src/eval/synthetic_specs.py — Step 17 evaluation spec fixture.

Covers:
1. build_synthetic_spec_store registers the correct number of specs.
2. Every registered spec has a non-None fingerprint extracted from the PDF.
3. The format-A family (format_a1, format_a2, format_a1_encrypted) maps to the
   same format_id and the fingerprint similarity between format_a1 and format_a2
   exceeds the pipeline's KNOWN match threshold (0.90).
4. The format-A-messy spec has its own format_id and a registered fingerprint.
5. The format-A-drift spec is registered with a separate format_id.
6. The format-B spec contains the expected anchor keywords and column mappings.
7. The format-C spec is an AIF spec with no holdings_table and the correct AIF
   summary field anchors.
8. A Pipeline configured with the synthetic SpecStore can process a format_a1
   PDF and return a status other than NEEDS_REVIEW (i.e., either PARSED or
   PARSED_WITH_WARNINGS — not all fields will match because the INR formatter
   in pdfplumber may produce different decimal representations, but the pipeline
   at least reaches the parsing stage).
9. An Evaluator configured with the synthetic spec pipeline returns
   processed_cases == total_cases for a single-case evaluation using
   format_a1.
10. build_synthetic_spec_store is idempotent: calling it twice with overwrite
    does not raise.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.eval.synthetic_specs import (
    _build_format_a_spec,
    _build_format_a_drift_spec,
    _build_format_b_spec,
    _build_format_c_spec,
    _locate_samples_dir,
    build_synthetic_spec_store,
)
from src.eval.evaluator import Evaluator
from src.eval.loader import load_case_from_ground_truth
from src.fingerprint.comparator import compare_fingerprints
from src.fingerprint.extractor import extract_fingerprint
from src.fingerprint.models import FingerprintMatchStatus
from src.ir.extractor import extract_pdf
from src.models.enums import DocumentStatus, StatementType
from src.pipeline.models import PipelineConfig
from src.pipeline.pipeline import Pipeline
from src.sample_generator.generator import ENCRYPTED_SAMPLE_PASSWORD
from src.specs.models import LocatorType
from src.specs.store import SpecStore


SAMPLES_DIR = _locate_samples_dir()

# Skip tests that require sample PDFs if they are not present
_SAMPLES_AVAILABLE = SAMPLES_DIR.is_dir() and any(SAMPLES_DIR.glob("*.pdf"))
requires_samples = pytest.mark.skipif(
    not _SAMPLES_AVAILABLE,
    reason="samples/synthetic PDFs not present",
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_spec_store() -> tuple[SpecStore, str]:
    """Return a fresh SpecStore in an isolated temporary directory."""
    tmp = tempfile.mkdtemp(prefix="test_synthetic_specs_")
    store = SpecStore(specs_dir=tmp)
    return store, tmp


# ---------------------------------------------------------------------------
# 1. Registration count
# ---------------------------------------------------------------------------

@requires_samples
def test_build_synthetic_spec_store_registers_five_unique_format_ids():
    """Five distinct format_ids should be registered: format-a, a-messy, a-drift, b, c."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    registered = store.list_formats()
    assert len(registered) == 5, f"Expected 5 specs, got {len(registered)}: {registered}"

    expected_ids = {
        "synthetic-format-a",
        "synthetic-format-a-messy",
        "synthetic-format-a-drift",
        "synthetic-format-b",
        "synthetic-format-c",
    }
    assert set(registered) == expected_ids


# ---------------------------------------------------------------------------
# 2. Fingerprints are non-None
# ---------------------------------------------------------------------------

@requires_samples
def test_all_registered_specs_have_fingerprints():
    """Every spec saved by build_synthetic_spec_store must carry a fingerprint."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    for fid in store.list_formats():
        spec = store.load(fid)
        assert spec.fingerprint is not None, f"Spec '{fid}' is missing a fingerprint"
        assert len(spec.fingerprint.anchor_phrases) > 0 or len(spec.fingerprint.table_headers) > 0, (
            f"Spec '{fid}' fingerprint is empty — extraction may have failed"
        )


# ---------------------------------------------------------------------------
# 3. Format-A family: format_a1 and format_a2 share a spec; fingerprint
#    similarity should be ≥ 0.90 (KNOWN match)
# ---------------------------------------------------------------------------

@requires_samples
def test_format_a1_and_format_a2_produce_known_fingerprint_similarity():
    """format_a1.pdf and format_a2.pdf should produce fingerprints that compare as KNOWN."""
    fp1 = extract_fingerprint(
        extract_pdf(SAMPLES_DIR / "format_a1.pdf"),
        format_id="test-a1",
    )
    fp2 = extract_fingerprint(
        extract_pdf(SAMPLES_DIR / "format_a2.pdf"),
        format_id="test-a2",
    )
    result = compare_fingerprints(fp1, fp2, match_threshold=0.90)
    assert result.status == FingerprintMatchStatus.KNOWN, (
        f"Expected KNOWN similarity between format_a1 and format_a2, "
        f"got {result.status} (score={result.similarity_score:.3f})"
    )


# ---------------------------------------------------------------------------
# 4. Format-A-messy has its own format_id
# ---------------------------------------------------------------------------

@requires_samples
def test_format_a_messy_registered_with_own_format_id():
    """format_a_messy must be registered as its own spec (not reusing format-a)."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    assert store.exists("synthetic-format-a-messy"), (
        "synthetic-format-a-messy not registered"
    )
    spec = store.load("synthetic-format-a-messy")
    assert spec.fingerprint is not None


# ---------------------------------------------------------------------------
# 5. Format-A-drift is separate
# ---------------------------------------------------------------------------

@requires_samples
def test_format_a_drift_registered_separately():
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    assert store.exists("synthetic-format-a-drift")
    spec = store.load("synthetic-format-a-drift")
    assert spec.fingerprint is not None
    # The drift spec must map "Net Value (INR)" not "Mkt Value (INR)"
    ht = spec.holdings_table
    assert ht is not None
    assert ht.column_mapping.get("market_value") == "Net Value (INR)"


# ---------------------------------------------------------------------------
# 6. Format-B spec structure
# ---------------------------------------------------------------------------

def test_format_b_spec_contains_expected_anchors_and_columns():
    """Format-B spec must reference the correct column names from format_b.py."""
    spec = _build_format_b_spec()

    assert spec.statement_type == StatementType.PMS
    assert spec.holdings_table is not None
    assert "Scrip Name" in spec.holdings_table.column_mapping.values()
    assert "ISIN Code" in spec.holdings_table.column_mapping.values()
    assert "Current Value" in spec.holdings_table.column_mapping.values()

    assert spec.transactions_table is not None
    assert "Trade Date" in spec.transactions_table.column_mapping.values()
    assert "Activity" in spec.transactions_table.column_mapping.values()
    assert "Consideration (INR)" in spec.transactions_table.column_mapping.values()

    # Metadata anchors
    assert spec.metadata_fields["client_identifier"].anchor == "Account Number:"
    assert spec.metadata_fields["statement_period"].anchor == "Report Period:"
    assert spec.metadata_fields["currency"].anchor == "Base Currency:"


# ---------------------------------------------------------------------------
# 7. Format-C spec structure (AIF)
# ---------------------------------------------------------------------------

def test_format_c_spec_is_aif_with_no_holdings_table():
    """Format-C spec must be AIF type, have no holdings_table, and carry AIF-specific anchors."""
    spec = _build_format_c_spec()

    assert spec.statement_type == StatementType.AIF
    assert spec.holdings_table is None, "AIF format should not have a holdings_table"

    # AIF summary anchors
    assert "units" in spec.summary_fields
    assert spec.summary_fields["units"].anchor == "Units Held"
    assert "nav_per_unit" in spec.summary_fields
    assert "closing_value" in spec.summary_fields
    assert spec.summary_fields["closing_value"].anchor == "Total Investment Value"

    # Cash Ledger transaction columns
    assert spec.transactions_table is not None
    assert spec.transactions_table.anchor == "Cash Ledger"
    assert "Activity" in spec.transactions_table.column_mapping.values()


# ---------------------------------------------------------------------------
# 8. Pipeline reaches parsing stage (not NEEDS_REVIEW) for format_a1
# ---------------------------------------------------------------------------

@requires_samples
def test_pipeline_with_synthetic_specs_reaches_parsing_for_format_a1():
    """Pipeline with the synthetic SpecStore must not return NEEDS_REVIEW for format_a1."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    pipeline = Pipeline(
        spec_store=store,
        config=PipelineConfig(store_results=False),
    )
    result = pipeline.process_file(SAMPLES_DIR / "format_a1.pdf")

    assert result.status != DocumentStatus.NEEDS_REVIEW, (
        f"Pipeline returned NEEDS_REVIEW for format_a1 — spec matching failed. "
        f"status={result.status}, errors={result.errors}"
    )
    # Should be PARSED or PARSED_WITH_WARNINGS
    assert result.status in (
        DocumentStatus.PARSED,
        DocumentStatus.PARSED_WITH_WARNINGS,
    ), f"Unexpected status: {result.status}"


# ---------------------------------------------------------------------------
# 9. Evaluator processes format_a1 case
# ---------------------------------------------------------------------------

@requires_samples
def test_evaluator_processes_format_a1_case():
    """Evaluator should report processed_cases == 1 for a format_a1 evaluation case."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    pipeline = Pipeline(
        spec_store=store,
        config=PipelineConfig(store_results=False),
    )
    evaluator = Evaluator(pipeline=pipeline)

    case = load_case_from_ground_truth(SAMPLES_DIR / "format_a1.json")
    eval_result = evaluator.evaluate_cases([case])

    assert eval_result.processed_cases == 1
    # Must have reached the parsing stage — not NEEDS_REVIEW
    case_result = eval_result.case_results[0]
    assert case_result.pipeline_status != DocumentStatus.NEEDS_REVIEW.value, (
        f"Case processed as NEEDS_REVIEW — spec matching failed: {case_result.errors}"
    )


# ---------------------------------------------------------------------------
# 10. Encrypted format_a1 pipeline test
# ---------------------------------------------------------------------------

@requires_samples
def test_pipeline_with_synthetic_specs_processes_encrypted_format_a1():
    """Pipeline should process the encrypted format_a1 PDF using the shared format-a spec."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    pipeline = Pipeline(
        spec_store=store,
        config=PipelineConfig(store_results=False),
    )
    result = pipeline.process_file(
        SAMPLES_DIR / "format_a1_encrypted.pdf",
        password=ENCRYPTED_SAMPLE_PASSWORD,
    )

    assert result.status != DocumentStatus.NEEDS_REVIEW, (
        f"Encrypted format_a1 returned NEEDS_REVIEW — spec matching failed. "
        f"status={result.status}"
    )


# ---------------------------------------------------------------------------
# 11. Format-C pipeline test
# ---------------------------------------------------------------------------

@requires_samples
def test_pipeline_with_synthetic_specs_processes_format_c():
    """Pipeline should not NEEDS_REVIEW the AIF format_c PDF."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)

    pipeline = Pipeline(
        spec_store=store,
        config=PipelineConfig(store_results=False),
    )
    result = pipeline.process_file(SAMPLES_DIR / "format_c.pdf")

    assert result.status != DocumentStatus.NEEDS_REVIEW, (
        f"AIF format_c returned NEEDS_REVIEW — spec matching failed. "
        f"status={result.status}"
    )
    assert result.status in (
        DocumentStatus.PARSED,
        DocumentStatus.PARSED_WITH_WARNINGS,
    )


# ---------------------------------------------------------------------------
# 12. Idempotency: calling twice does not raise
# ---------------------------------------------------------------------------

@requires_samples
def test_build_synthetic_spec_store_is_idempotent():
    """Calling build_synthetic_spec_store twice on the same store must not raise."""
    store, _ = _make_spec_store()
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)
    # Second call with overwrite=True inside — must not raise DuplicateSpecError
    build_synthetic_spec_store(store, samples_dir=SAMPLES_DIR)
    assert len(store.list_formats()) == 5


# ---------------------------------------------------------------------------
# 13. Unit test: spec-builder functions work without PDFs
# ---------------------------------------------------------------------------

def test_spec_builders_construct_valid_specs_without_pdfs():
    """All spec builders return valid ParsingSpec objects (no PDF extraction needed)."""
    spec_a = _build_format_a_spec("test-a")
    assert spec_a.format_id == "test-a"
    assert spec_a.statement_type == StatementType.PMS
    assert "client_identifier" in spec_a.metadata_fields
    assert spec_a.holdings_table is not None
    assert spec_a.transactions_table is not None

    spec_drift = _build_format_a_drift_spec("test-a-drift")
    assert spec_drift.holdings_table.column_mapping["market_value"] == "Net Value (INR)"

    spec_b = _build_format_b_spec("test-b")
    assert spec_b.holdings_table.anchor == "Portfolio Holdings"

    spec_c = _build_format_c_spec("test-c")
    assert spec_c.statement_type == StatementType.AIF
    assert spec_c.holdings_table is None
