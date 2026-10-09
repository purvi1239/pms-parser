"""Tests for Step 3: PDF extraction → Intermediate Representation (IR).

Covers:
  1. Extracting a generated Format A PDF (DocumentIR structure, pages, dimensions).
  2. Text extraction (raw_text, text_blocks, words, bounding boxes, reading order).
  3. Table extraction (headers, rows, cells, bounding boxes).
  4. Multiple pages preservation (page numbers 1..N, per-page dimensions).
  5. Page without a table (clean empty tables list, text preserved).
  6. Invalid / unreadable PDF handling (missing file, 0 bytes, corrupted data).
  7. Password-protected PDF handling (fails without password, succeeds with correct password).
  8. Extraction of Format B and Format C samples.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table

from src.ir import (
    BBox,
    DocumentIR,
    PageIR,
    PDFExtractionError,
    TableIR,
    TextBlock,
    WordIR,
    extract_pdf,
)
from src.sample_generator import (
    ENCRYPTED_SAMPLE_LABEL,
    ENCRYPTED_SAMPLE_PASSWORD,
    generate_all,
)


@pytest.fixture(scope="module")
def samples_dir(tmp_path_factory) -> Path:
    """Ensure all synthetic PDF samples are generated in a clean temporary directory."""
    out = tmp_path_factory.mktemp("synthetic_ir_test")
    generate_all(out_dir=out)
    return out


# ---------------------------------------------------------------------------
# 1. Format A PDF extraction & DocumentIR structure
# ---------------------------------------------------------------------------

def test_extract_format_a_pdf(samples_dir):
    """Extract format_a1.pdf and verify DocumentIR structure."""
    pdf_path = samples_dir / "format_a1.pdf"
    doc_ir = extract_pdf(pdf_path)

    assert isinstance(doc_ir, DocumentIR)
    assert doc_ir.source_path == str(pdf_path)
    assert doc_ir.page_count == 1
    assert len(doc_ir.pages) == 1

    page = doc_ir.pages[0]
    assert isinstance(page, PageIR)
    assert page.page_number == 1
    # A4 dimensions are ~595.28 x 841.89 points
    assert pytest.approx(page.width, rel=1e-2) == 595.28
    assert pytest.approx(page.height, rel=1e-2) == 841.89


# ---------------------------------------------------------------------------
# 2. Text extraction & spatial tokens
# ---------------------------------------------------------------------------

def test_text_extraction(samples_dir):
    """Verify raw text, text blocks, and word tokens."""
    pdf_path = samples_dir / "format_a1.pdf"
    doc_ir = extract_pdf(pdf_path)
    page = doc_ir.pages[0]

    # Raw text
    raw_text = page.get_text()
    assert "Portfolio Management Statement" in raw_text
    assert "Pinnacle Wealth PMS" in raw_text
    assert "PWM-CLIENT-0042" in raw_text

    # Document-level text aggregator
    all_text = doc_ir.get_all_text()
    assert "Portfolio Management Statement" in all_text

    # Text blocks (lines)
    assert len(page.text_blocks) > 0
    first_block = page.text_blocks[0]
    assert isinstance(first_block, TextBlock)
    assert first_block.page_number == 1
    assert "Portfolio Management Statement" in first_block.text
    assert first_block.bbox is not None
    assert first_block.bbox.width > 0
    assert first_block.bbox.height > 0

    # Word tokens
    assert len(page.words) > 0
    word_texts = [w.text for w in page.words]
    assert "Portfolio" in word_texts
    assert "Statement" in word_texts

    for w in page.words[:10]:
        assert isinstance(w, WordIR)
        assert w.page_number == 1
        assert w.bbox.x0 < w.bbox.x1
        assert w.bbox.top < w.bbox.bottom


# ---------------------------------------------------------------------------
# 3. Table extraction
# ---------------------------------------------------------------------------

def test_table_extraction(samples_dir):
    """Verify table extraction, headers, rows, cells, and bounding boxes."""
    pdf_path = samples_dir / "format_a1.pdf"
    doc_ir = extract_pdf(pdf_path)
    page = doc_ir.pages[0]

    # Format A has holdings table, transactions table, summary table
    assert len(page.tables) >= 2
    all_tables = doc_ir.get_all_tables()
    assert len(all_tables) == len(page.tables)

    # Inspect first table (holdings)
    holdings_tbl = page.tables[0]
    assert isinstance(holdings_tbl, TableIR)
    assert holdings_tbl.page_number == 1
    assert holdings_tbl.bbox is not None
    assert holdings_tbl.row_count > 1
    assert holdings_tbl.col_count >= 5

    headers = holdings_tbl.get_headers()
    assert "Security Name" in headers
    assert "ISIN" in headers

    data_rows = holdings_tbl.get_data_rows()
    assert len(data_rows) == holdings_tbl.row_count - 1
    # Check that a security name is in data rows
    joined_data = " ".join(" ".join(r) for r in data_rows)
    assert "Reliance Industries" in joined_data

    # Check cell objects
    assert len(holdings_tbl.cells) > 0
    c0 = holdings_tbl.cells[0]
    assert c0.row_index == 0
    assert c0.col_index == 0
    assert c0.text == headers[0]


# ---------------------------------------------------------------------------
# 4 & 5. Multiple pages and page without a table
# ---------------------------------------------------------------------------

def test_multi_page_and_page_without_table(tmp_path):
    """Verify multiple pages, reading order, and page with no table."""
    multi_pdf = tmp_path / "multi_page_test.pdf"

    # Create a 2-page PDF:
    # Page 1: heading + table
    # Page 2: text only (no table)
    doc = SimpleDocTemplate(str(multi_pdf), pagesize=letter)
    styles = getSampleStyleSheet()

    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    tbl = Table([["Col A", "Col B"], ["Val 1", "Val 2"]])
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))

    story = [
        Paragraph("First Page Header", styles["Heading1"]),
        tbl,
        PageBreak(),
        Paragraph("Second Page Header with No Tables", styles["Heading1"]),
        Paragraph("Additional paragraph on second page.", styles["Normal"]),
    ]
    doc.build(story)

    doc_ir = extract_pdf(multi_pdf)

    assert doc_ir.page_count == 2
    assert len(doc_ir.pages) == 2

    # Page 1 checks
    p1 = doc_ir.get_page(1)
    assert p1.page_number == 1
    assert "First Page Header" in p1.get_text()
    assert len(p1.tables) == 1
    assert p1.tables[0].get_headers() == ["Col A", "Col B"]

    # Page 2 checks (no tables)
    p2 = doc_ir.get_page(2)
    assert p2.page_number == 2
    assert "Second Page Header with No Tables" in p2.get_text()
    assert p2.tables == []

    # Out of bounds page lookup
    with pytest.raises(IndexError):
        doc_ir.get_page(3)


# ---------------------------------------------------------------------------
# 6. Invalid / unreadable PDF handling
# ---------------------------------------------------------------------------

def test_non_existent_file_raises_error(tmp_path):
    """Non-existent file must raise PDFExtractionError."""
    missing = tmp_path / "does_not_exist.pdf"
    with pytest.raises(PDFExtractionError, match="does not exist"):
        extract_pdf(missing)


def test_directory_path_raises_error(tmp_path):
    """Passing a directory must raise PDFExtractionError."""
    with pytest.raises(PDFExtractionError, match="not a file"):
        extract_pdf(tmp_path)


def test_empty_file_raises_error(tmp_path):
    """Empty 0-byte file must raise PDFExtractionError."""
    empty_file = tmp_path / "empty.pdf"
    empty_file.write_bytes(b"")
    with pytest.raises(PDFExtractionError, match="empty"):
        extract_pdf(empty_file)


def test_corrupted_file_raises_error(tmp_path):
    """Corrupted non-PDF bytes must raise PDFExtractionError."""
    corrupt_file = tmp_path / "corrupt.pdf"
    corrupt_file.write_bytes(b"NOT A VALID PDF FILE CONTENT AT ALL")
    with pytest.raises(PDFExtractionError):
        extract_pdf(corrupt_file)


# ---------------------------------------------------------------------------
# 7. Password-protected PDF handling
# ---------------------------------------------------------------------------

def test_encrypted_pdf_fails_without_password(samples_dir):
    """Encrypted PDF without password must raise PDFExtractionError."""
    enc_path = samples_dir / f"{ENCRYPTED_SAMPLE_LABEL}.pdf"
    with pytest.raises(PDFExtractionError):
        extract_pdf(enc_path)


def test_encrypted_pdf_succeeds_with_correct_password(samples_dir):
    """Encrypted PDF with correct password must extract successfully."""
    enc_path = samples_dir / f"{ENCRYPTED_SAMPLE_LABEL}.pdf"
    doc_ir = extract_pdf(enc_path, password=ENCRYPTED_SAMPLE_PASSWORD)

    assert doc_ir.page_count == 1
    assert "Portfolio Management Statement" in doc_ir.get_all_text()
    assert len(doc_ir.pages[0].tables) >= 2


# ---------------------------------------------------------------------------
# 8. Format B and Format C extraction
# ---------------------------------------------------------------------------

def test_extract_format_b(samples_dir):
    """Verify extraction of Format B (structurally different PMS)."""
    b_path = samples_dir / "format_b.pdf"
    doc_ir = extract_pdf(b_path)

    assert doc_ir.page_count == 1
    all_text = doc_ir.get_all_text()
    assert "Client Portfolio Report" in all_text
    assert "Horizon Capital Management" in all_text
    assert len(doc_ir.pages[0].tables) >= 2


def test_extract_format_c(samples_dir):
    """Verify extraction of Format C (AIF statement)."""
    c_path = samples_dir / "format_c.pdf"
    doc_ir = extract_pdf(c_path)

    assert doc_ir.page_count == 1
    all_text = doc_ir.get_all_text()
    assert "Alternative Investment Fund" in all_text
    assert "Nexus Ventures AIF" in all_text
    assert len(doc_ir.pages[0].tables) >= 2
