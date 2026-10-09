"""Tests for Step 4: Document Classifier.

Covers:
  1. Generated Format A PMS sample classified as STATEMENT (PMS).
  2. Generated Format B PMS sample classified as STATEMENT (PMS).
  3. Generated Format C AIF sample classified as STATEMENT (AIF).
  4. Clearly irrelevant documents classified as SKIP.
  5. Single generic financial term does not classify as statement (SKIP).
  6. Ambiguous financial document classified as MAYBE.
  7. Empty or whitespace-only documents handled safely (SKIP).
  8. Classification result structure (confidence, score, reasons, signals).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.classifier import (
    ClassificationDecision,
    ClassificationResult,
    classify_document,
)
from src.ir import DocumentIR, PageIR, TextBlock, extract_pdf
from src.models.enums import StatementType
from src.sample_generator import generate_all


@pytest.fixture(scope="module")
def samples_dir(tmp_path_factory) -> Path:
    """Generate all synthetic samples in a temporary directory for tests."""
    out = tmp_path_factory.mktemp("synthetic_classifier_test")
    generate_all(out_dir=out)
    return out


# ---------------------------------------------------------------------------
# 1. Format A (PMS) classification
# ---------------------------------------------------------------------------

def test_format_a_classified_as_pms_statement(samples_dir):
    """Format A must be classified as STATEMENT with StatementType.PMS."""
    doc_ir = extract_pdf(samples_dir / "format_a1.pdf")
    result = classify_document(doc_ir)

    assert isinstance(result, ClassificationResult)
    assert result.decision == ClassificationDecision.STATEMENT
    assert result.statement_type == StatementType.PMS
    assert result.score >= 50
    assert result.confidence >= 0.80
    assert len(result.reasons) > 0


def test_format_a_variants_classified_as_pms(samples_dir):
    """Format A messy and drift variants must both be recognized as PMS statements."""
    for variant in ["format_a_messy.pdf", "format_a_drift.pdf"]:
        doc_ir = extract_pdf(samples_dir / variant)
        result = classify_document(doc_ir)
        assert result.decision == ClassificationDecision.STATEMENT
        assert result.statement_type == StatementType.PMS
        assert result.score >= 50


# ---------------------------------------------------------------------------
# 2. Format B (PMS) classification
# ---------------------------------------------------------------------------

def test_format_b_classified_as_pms_statement(samples_dir):
    """Format B must be classified as STATEMENT with StatementType.PMS."""
    doc_ir = extract_pdf(samples_dir / "format_b.pdf")
    result = classify_document(doc_ir)

    assert result.decision == ClassificationDecision.STATEMENT
    assert result.statement_type == StatementType.PMS
    assert result.score >= 50
    assert result.confidence >= 0.80
    assert len(result.reasons) > 0


# ---------------------------------------------------------------------------
# 3. Format C (AIF) classification
# ---------------------------------------------------------------------------

def test_format_c_classified_as_aif_statement(samples_dir):
    """Format C must be classified as STATEMENT with StatementType.AIF."""
    doc_ir = extract_pdf(samples_dir / "format_c.pdf")
    result = classify_document(doc_ir)

    assert result.decision == ClassificationDecision.STATEMENT
    assert result.statement_type == StatementType.AIF
    assert result.score >= 50
    assert result.confidence >= 0.80
    assert len(result.reasons) > 0


# ---------------------------------------------------------------------------
# 4. Irrelevant documents classified as SKIP
# ---------------------------------------------------------------------------

def test_clearly_irrelevant_document_classified_as_skip():
    """Non-financial documents (e.g. invoice or general note) must be classified as SKIP."""
    irrelevant_doc = DocumentIR(
        source_path="invoice.pdf",
        page_count=1,
        pages=[
            PageIR(
                page_number=1,
                width=595.0,
                height=842.0,
                raw_text="Tax Invoice\nBill To: John Doe\nShipping Address: 123 Tech Park\nTotal: $45.00",
                text_blocks=[
                    TextBlock(text="Tax Invoice", page_number=1),
                    TextBlock(text="Bill To: John Doe", page_number=1),
                ],
            )
        ],
    )
    result = classify_document(irrelevant_doc)

    assert result.decision == ClassificationDecision.SKIP
    assert result.statement_type is None
    assert result.score < 25
    assert result.confidence <= 0.30


def test_recipe_document_classified_as_skip():
    """Completely unrelated text must be classified as SKIP."""
    recipe_doc = DocumentIR(
        source_path="recipe.pdf",
        page_count=1,
        pages=[
            PageIR(
                page_number=1,
                width=595.0,
                height=842.0,
                raw_text="Grandma's Chocolate Chip Cookie Recipe\nIngredients: 2 cups flour, 1 cup sugar, chocolate chips.\nBake at 350F for 12 minutes.",
            )
        ],
    )
    result = classify_document(recipe_doc)

    assert result.decision == ClassificationDecision.SKIP
    assert result.statement_type is None
    assert result.score == 0


# ---------------------------------------------------------------------------
# 5. Generic financial words alone must not classify as STATEMENT
# ---------------------------------------------------------------------------

def test_single_generic_financial_word_does_not_trigger_statement():
    """A single financial word like 'investment' or 'dividend' alone must produce SKIP."""
    doc = DocumentIR(
        source_path="meeting_notes.pdf",
        page_count=1,
        pages=[
            PageIR(
                page_number=1,
                width=595.0,
                height=842.0,
                raw_text="Team meeting notes: Please review the dividend policy presentation for next week.",
            )
        ],
    )
    result = classify_document(doc)

    assert result.decision == ClassificationDecision.SKIP
    assert result.statement_type is None
    assert result.score < 25


# ---------------------------------------------------------------------------
# 6. Ambiguous document classified as MAYBE
# ---------------------------------------------------------------------------

def test_ambiguous_document_classified_as_maybe():
    """Document with some statement anchors but missing full structure must be classified as MAYBE."""
    # Has a statement anchor ('Statement Period') and some financial terms,
    # but no strong PMS/AIF markers and no structured tables.
    ambiguous_doc = DocumentIR(
        source_path="ambiguous_notice.pdf",
        page_count=1,
        pages=[
            PageIR(
                page_number=1,
                width=595.0,
                height=842.0,
                raw_text=(
                    "Account Summary Notice\n"
                    "Statement Period: January 2026\n"
                    "Here is a summary of your recent transactions and fees charged.\n"
                    "Please contact customer care if you have questions."
                ),
            )
        ],
    )
    result = classify_document(ambiguous_doc)

    assert result.decision == ClassificationDecision.MAYBE
    assert 25 <= result.score < 50
    assert 0.40 <= result.confidence <= 0.70
    assert any("manual review" in r.lower() or "ambiguous" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# 7. Empty or whitespace-only documents handled safely
# ---------------------------------------------------------------------------

def test_empty_document_handled_safely():
    """Document with 0 pages must return SKIP with score 0."""
    empty_doc = DocumentIR(page_count=0, pages=[])
    result = classify_document(empty_doc)

    assert result.decision == ClassificationDecision.SKIP
    assert result.statement_type is None
    assert result.score == 0
    assert result.confidence == 0.0


def test_whitespace_only_document_handled_safely():
    """Document with only whitespace text must return SKIP with score 0."""
    ws_doc = DocumentIR(
        page_count=1,
        pages=[PageIR(page_number=1, width=595.0, height=842.0, raw_text="   \n\n\t  ")],
    )
    result = classify_document(ws_doc)

    assert result.decision == ClassificationDecision.SKIP
    assert result.statement_type is None
    assert result.score == 0
    assert result.confidence == 0.0
