"""Feature extraction from DocumentIR to construct structural FormatFingerprint."""
from __future__ import annotations

import re
from typing import Any

from src.fingerprint.models import FormatFingerprint, LayoutStats
from src.ir.models import DocumentIR


def _normalize_text(text: str) -> str:
    """Normalize a text phrase: lowercased, stripped, collapsed spaces."""
    cleaned = re.sub(r"[^\w\s%]", " ", text.lower())
    return " ".join(cleaned.split())


def _contains_data_value(norm: str) -> bool:
    """Return True if the normalized phrase contains embedded numeric/amount tokens.

    Catches patterns produced by currency amounts after normalization, e.g.:
      - "₹80,000.00"  → "80 000 00" (pure digit words)
      - "N80,000.00"  → "n80 000 00" (currency-letter prefix + digits)
    Such tokens indicate the phrase is a data-row value, not a structural label.
    """
    for word in norm.split():
        # Bare digit sequences (e.g. "80", "000", "00")
        if re.fullmatch(r"\d+", word):
            return True
        # Letter-prefixed amounts (e.g. "n80", "n1200") — INR symbol artefacts
        if re.fullmatch(r"[a-z]\d+", word):
            return True
    return False


def _is_structural_header_row(norm_cells: list[str]) -> bool:
    """Return True only if all non-empty cells look like column-label text.

    A structural header row (e.g. "Security Name | ISIN | Qty | Cost (INR)")
    should have no cells that are pure data values (numbers, amounts).
    2-column label-value tables (e.g. summary tables) are also excluded because
    their first row is a data row, not a column definition.
    """
    # Require at least 3 columns — 2-column tables are summary label/value pairs
    if len(norm_cells) < 3:
        return False
    # Every cell must pass the data-value filter
    return not any(_contains_data_value(cell) for cell in norm_cells)


def _is_static_anchor_candidate(text: str) -> bool:
    """Return True if *text* is a structural layout anchor (heading, label key).

    Rejects:
      - Empty or whitespace-only strings
      - Phrases that are purely numbers/dates
      - Phrases longer than 6 words (transaction rows, wrapped security names)
      - Phrases that embed numeric data values (e.g. summary row text like
        "Opening Cash Balance ₹80,000.00" → "opening cash balance n80 000 00")
    """
    norm = _normalize_text(text)
    if not norm:
        return False
    # Purely numeric / date-like tokens
    if re.fullmatch(r"[\d\s,./\-]+", norm):
        return False
    words = norm.split()
    # Structural headings are 1–6 words; longer phrases are data rows
    if len(words) > 6:
        return False
    # Reject if any word is a standalone number or a letter-prefixed amount
    if _contains_data_value(norm):
        return False
    return True


def extract_fingerprint(doc: DocumentIR, format_id: str | None = None) -> FormatFingerprint:
    """Extract structural layout features and text anchors from DocumentIR.

    Features extracted
    ------------------
    anchor_phrases
        Normalized structural text labels found in the document (section headings,
        metadata keys).  Data-row values (amounts, client IDs, dates) are excluded
        so the same template with different data produces identical anchors.

    table_headers
        Ordered list of column-header rows for every multi-column (≥3 col) table
        whose first row looks like a structural header (no numeric cells).
        2-column summary tables and data tables without a label-style header are
        excluded to prevent data-value contamination.

    layout_stats
        Page count, fingerprinted table count, and column-count sequence.

    Args:
        doc: The intermediate representation of the document.
        format_id: Optional format identifier to assign to this fingerprint.

    Returns:
        FormatFingerprint containing anchor phrases, table headers, and layout stats.
    """
    if doc.page_count == 0 or not doc.pages:
        return FormatFingerprint(
            format_id=format_id,
            anchor_phrases=[],
            table_headers=[],
            layout_stats=LayoutStats(page_count=0, table_count=0, column_counts=[]),
        )

    # ── 1. Extract structural text anchors ────────────────────────────────────
    seen_anchors: set[str] = set()
    ordered_anchors: list[str] = []

    for page in doc.pages:
        for block in page.text_blocks:
            line = block.text.strip()
            if not line:
                continue

            if ":" in line:
                # Split metadata lines at ":" and capture each key part
                for part in line.split(":"):
                    norm_part = _normalize_text(part)
                    if _is_static_anchor_candidate(norm_part) and norm_part not in seen_anchors:
                        seen_anchors.add(norm_part)
                        ordered_anchors.append(norm_part)
            else:
                norm_line = _normalize_text(line)
                if _is_static_anchor_candidate(norm_line) and norm_line not in seen_anchors:
                    seen_anchors.add(norm_line)
                    ordered_anchors.append(norm_line)

    # ── 2. Extract table headers (structural tables only) ─────────────────────
    raw_tables = doc.get_all_tables()
    cleaned_table_headers: list[list[str]] = []
    col_counts: list[int] = []

    for tbl in raw_tables:
        raw_headers = tbl.get_headers()
        norm_headers = [_normalize_text(h) for h in raw_headers if h.strip()]
        if not norm_headers:
            continue

        # Skip 2-column summary/label-value tables and tables whose "header"
        # row contains data amounts rather than column labels.
        if not _is_structural_header_row(norm_headers):
            continue

        # Dedup consecutive duplicate headers arising from multi-page table splits
        if cleaned_table_headers and cleaned_table_headers[-1] == norm_headers:
            continue

        cleaned_table_headers.append(norm_headers)
        col_counts.append(len(norm_headers))

    # ── 3. Layout statistics ──────────────────────────────────────────────────
    layout = LayoutStats(
        page_count=doc.page_count,
        table_count=len(cleaned_table_headers),
        column_counts=col_counts,
    )

    metadata: dict[str, Any] = {
        "source_path": doc.source_path,
        "unique_anchor_count": len(ordered_anchors),
    }

    return FormatFingerprint(
        format_id=format_id,
        anchor_phrases=ordered_anchors,
        table_headers=cleaned_table_headers,
        layout_stats=layout,
        metadata=metadata,
    )
