"""Tests for Step 5: Format Fingerprinting and Drift Detection.

Covers:
  1. Identical documents (score = 1.0, status = KNOWN).
  2. Similar documents with same format / different data (Format A1 vs Format A2 -> KNOWN).
  3. Multi-page document with repeated headers (Format A1 vs Format A-messy -> KNOWN).
  4. Changed-header & column-order template mutation (Format A1 vs Format A-drift -> DRIFT_SUSPECTED).
  5. Unrelated document layouts (Format A1 vs Format B, Format A1 vs Format C -> UNKNOWN).
  6. Sparse or empty documents handled safely without errors.
  7. Multi-template library matching via match_fingerprint.
  8. Configurable thresholds customization.
  9. Structural feature verification (no plain file hashes).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.fingerprint import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
    compare_fingerprints,
    extract_fingerprint,
    match_fingerprint,
)
from src.ir import DocumentIR, PageIR, extract_pdf
from src.sample_generator import generate_all


@pytest.fixture(scope="module")
def samples_dir(tmp_path_factory) -> Path:
    """Ensure all synthetic PDF samples are generated in a clean temporary directory."""
    out = tmp_path_factory.mktemp("synthetic_fingerprint_test")
    generate_all(out_dir=out)
    return out


@pytest.fixture(scope="module")
def sample_fingerprints(samples_dir) -> dict[str, FormatFingerprint]:
    """Extract fingerprints for all synthetic sample PDFs once for tests."""
    fps: dict[str, FormatFingerprint] = {}
    for name in [
        "format_a1",
        "format_a2",
        "format_a_messy",
        "format_a_drift",
        "format_b",
        "format_c",
    ]:
        pdf_path = samples_dir / f"{name}.pdf"
        doc = extract_pdf(pdf_path)
        fps[name] = extract_fingerprint(doc, format_id=name)
    return fps


# ---------------------------------------------------------------------------
# 1. Structural features verification (no plain hash)
# ---------------------------------------------------------------------------

def test_fingerprint_extracts_structured_features(sample_fingerprints):
    """Verify that fingerprint contains explicit anchors, table headers, and layout stats."""
    fp = sample_fingerprints["format_a1"]

    assert isinstance(fp, FormatFingerprint)
    assert len(fp.anchor_phrases) > 0
    assert "portfolio management statement" in fp.anchor_phrases
    assert "holdings" in fp.anchor_phrases
    assert "transactions" in fp.anchor_phrases

    assert len(fp.table_headers) >= 2
    # Holdings table header columns are preserved
    assert "security name" in fp.table_headers[0]
    assert "isin" in fp.table_headers[0]

    assert fp.layout_stats.page_count == 1
    assert fp.layout_stats.table_count >= 2
    assert len(fp.layout_stats.column_counts) >= 2


# ---------------------------------------------------------------------------
# 2. Identical document comparison
# ---------------------------------------------------------------------------

def test_identical_fingerprints_produce_known(sample_fingerprints):
    """Comparing a fingerprint with itself must produce score 1.0 and KNOWN."""
    fp_a1 = sample_fingerprints["format_a1"]
    res = compare_fingerprints(fp_a1, fp_a1)

    assert isinstance(res, FingerprintMatchResult)
    assert res.status == FingerprintMatchStatus.KNOWN
    assert res.similarity_score == 1.0
    assert res.component_scores["table_header"] == 1.0
    assert res.component_scores["anchor"] == 1.0
    assert res.component_scores["layout"] == 1.0


# ---------------------------------------------------------------------------
# 3. Similar documents (different data, same layout)
# ---------------------------------------------------------------------------

def test_same_format_different_quarter_produces_known(sample_fingerprints):
    """Format A1 and Format A2 have different stocks/numbers but identical layout -> KNOWN."""
    fp_a1 = sample_fingerprints["format_a1"]
    fp_a2 = sample_fingerprints["format_a2"]
    res = compare_fingerprints(fp_a1, fp_a2)

    assert res.status == FingerprintMatchStatus.KNOWN
    assert res.similarity_score >= 0.95
    assert res.component_scores["table_header"] >= 0.95


# ---------------------------------------------------------------------------
# 4. Multi-page document with repeated headers (Format A-messy)
# ---------------------------------------------------------------------------

def test_format_a_messy_matches_format_a_as_known(sample_fingerprints):
    """Format A-messy shares Format A's table structure and anchors -> KNOWN."""
    fp_a1 = sample_fingerprints["format_a1"]
    fp_messy = sample_fingerprints["format_a_messy"]
    res = compare_fingerprints(fp_messy, fp_a1)

    assert res.status == FingerprintMatchStatus.KNOWN
    assert res.similarity_score >= 0.95


# ---------------------------------------------------------------------------
# 5. Changed header & column order (Format A-drift -> DRIFT_SUSPECTED)
# ---------------------------------------------------------------------------

def test_format_a_drift_triggers_drift_suspected(sample_fingerprints):
    """Mutated Format A (renamed/reordered columns) must trigger DRIFT_SUSPECTED."""
    fp_a1 = sample_fingerprints["format_a1"]
    fp_drift = sample_fingerprints["format_a_drift"]
    res = compare_fingerprints(fp_drift, fp_a1)

    assert res.status == FingerprintMatchStatus.DRIFT_SUSPECTED
    # Score should be within near_match (0.65) and match (0.90)
    assert 0.65 <= res.similarity_score < 0.90
    assert any("drift" in r.lower() for r in res.reasons)


# ---------------------------------------------------------------------------
# 6. Unrelated formats -> UNKNOWN
# ---------------------------------------------------------------------------

def test_unrelated_formats_produce_unknown(sample_fingerprints):
    """Format B and Format C compared against Format A must produce UNKNOWN."""
    fp_a1 = sample_fingerprints["format_a1"]
    fp_b = sample_fingerprints["format_b"]
    fp_c = sample_fingerprints["format_c"]

    res_b = compare_fingerprints(fp_b, fp_a1)
    assert res_b.status == FingerprintMatchStatus.UNKNOWN
    assert res_b.similarity_score < 0.30

    res_c = compare_fingerprints(fp_c, fp_a1)
    assert res_c.status == FingerprintMatchStatus.UNKNOWN
    assert res_c.similarity_score < 0.30


# ---------------------------------------------------------------------------
# 7. Sparse or empty documents
# ---------------------------------------------------------------------------

def test_sparse_or_empty_documents_handled_safely(sample_fingerprints):
    """Empty or whitespace documents must compare safely without exceptions."""
    empty_doc = DocumentIR(page_count=0, pages=[])
    sparse_fp = extract_fingerprint(empty_doc, format_id="empty")

    fp_a1 = sample_fingerprints["format_a1"]
    res = compare_fingerprints(sparse_fp, fp_a1)

    assert res.status == FingerprintMatchStatus.UNKNOWN
    assert res.similarity_score == 0.0

    # Two empty fingerprints match each other
    empty_res = compare_fingerprints(sparse_fp, sparse_fp)
    assert empty_res.status == FingerprintMatchStatus.KNOWN
    assert empty_res.similarity_score == 1.0


# ---------------------------------------------------------------------------
# 8. Library matching via match_fingerprint
# ---------------------------------------------------------------------------

def test_match_fingerprint_against_library(sample_fingerprints):
    """match_fingerprint finds the correct format template from a catalog."""
    library = {
        "fmt_a": sample_fingerprints["format_a1"],
        "fmt_b": sample_fingerprints["format_b"],
        "fmt_c": sample_fingerprints["format_c"],
    }

    # Query with A2 -> matches fmt_a as KNOWN
    match_a2 = match_fingerprint(sample_fingerprints["format_a2"], library)
    assert match_a2.status == FingerprintMatchStatus.KNOWN
    assert match_a2.matched_format_id == "format_a1"

    # Query with A-messy -> matches fmt_a as KNOWN
    match_messy = match_fingerprint(sample_fingerprints["format_a_messy"], library)
    assert match_messy.status == FingerprintMatchStatus.KNOWN
    assert match_messy.matched_format_id == "format_a1"

    # Query with A-drift -> triggers DRIFT_SUSPECTED against fmt_a
    match_drift = match_fingerprint(sample_fingerprints["format_a_drift"], library)
    assert match_drift.status == FingerprintMatchStatus.DRIFT_SUSPECTED
    assert match_drift.matched_format_id == "format_a1"

    # Query with an empty document -> UNKNOWN and no matched format id
    empty_fp = extract_fingerprint(DocumentIR(page_count=0, pages=[]))
    match_empty = match_fingerprint(empty_fp, library)
    assert match_empty.status == FingerprintMatchStatus.UNKNOWN
    assert match_empty.matched_format_id is None


# ---------------------------------------------------------------------------
# 9. Configurable thresholds
# ---------------------------------------------------------------------------

def test_configurable_thresholds_override(sample_fingerprints):
    """Custom match_threshold and near_match_threshold adjust classification bounds."""
    fp_a1 = sample_fingerprints["format_a1"]
    fp_drift = sample_fingerprints["format_a_drift"]

    # When match_threshold is set very low (e.g. 0.70), drift promotes to KNOWN
    res_low = compare_fingerprints(fp_drift, fp_a1, match_threshold=0.70)
    assert res_low.status == FingerprintMatchStatus.KNOWN

    # When near_match_threshold is set very high (e.g. 0.85), drift becomes UNKNOWN
    res_high = compare_fingerprints(fp_drift, fp_a1, near_match_threshold=0.85)
    assert res_high.status == FingerprintMatchStatus.UNKNOWN
