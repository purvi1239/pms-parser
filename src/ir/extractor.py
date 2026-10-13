"""PDF extractor producing Intermediate Representation (DocumentIR) via pdfplumber.

Preserves text, spatial lines/blocks, word tokens with bounding boxes, and tables.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pdfplumber

from src.ir.exceptions import PDFExtractionError
from src.ir.models import (
    BBox,
    DocumentIR,
    PageIR,
    TableCell,
    TableIR,
    TextBlock,
    WordIR,
)


def extract_pdf(path: str | Path, password: str | None = None) -> DocumentIR:
    """Extract structural layout, text blocks, and tables from a PDF file.

    Args:
        path: File path to the PDF document.
        password: Optional password for encrypted PDF files.

    Returns:
        DocumentIR containing parsed pages, text blocks, words, and tables.

    Raises:
        PDFExtractionError: If the file does not exist, is empty, cannot be opened,
                            requires a password, or is otherwise invalid/unreadable.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise PDFExtractionError(f"PDF file does not exist: {file_path}")
    if not file_path.is_file():
        raise PDFExtractionError(f"Specified path is not a file: {file_path}")
    if file_path.stat().st_size == 0:
        raise PDFExtractionError(f"PDF file is empty (0 bytes): {file_path}")

    try:
        pdf_ctx = pdfplumber.open(str(file_path), password=password)
    except Exception as err:
        err_msg = str(err)
        if "Password" in err_msg or "encrypt" in err_msg.lower():
            raise PDFExtractionError(
                f"PDF is password-protected or supplied password is invalid for '{file_path}': {err}"
            ) from err
        raise PDFExtractionError(f"Failed to open PDF file '{file_path}': {err}") from err

    pages_ir: list[PageIR] = []

    with pdf_ctx as pdf:
        try:
            raw_metadata: dict[str, Any] = dict(pdf.metadata) if pdf.metadata else {}
            # Ensure metadata keys/values are basic serializable types
            clean_metadata = {
                str(k): (str(v) if v is not None else None)
                for k, v in raw_metadata.items()
            }

            for idx, page in enumerate(pdf.pages):
                page_num = idx + 1
                width = float(page.width)
                height = float(page.height)
                # Normalize font-fallback glyphs: ReportLab maps the Rupee symbol ₹ (U+20B9)
                # to character code 'n' in ZapfDingbats when rendering standard Type 1 fonts.
                # Normalizing this in page.chars before text, word, and table extraction ensures
                # raw_text, words, text_blocks, and tables faithfully reconstruct the currency symbol.
                if hasattr(page, "chars") and page.chars:
                    for c in page.chars:
                        if "dingbats" in str(c.get("fontname", "")).lower() and c.get("text") == "n":
                            c["text"] = "₹"

                raw_text = page.extract_text() or ""

                # Extract words with bounding boxes
                words_ir: list[WordIR] = []
                try:
                    words_raw = page.extract_words() or []
                    for w in words_raw:
                        words_ir.append(
                            WordIR(
                                text=str(w.get("text", "")),
                                page_number=page_num,
                                bbox=BBox(
                                    x0=float(w.get("x0", 0.0)),
                                    top=float(w.get("top", 0.0)),
                                    x1=float(w.get("x1", 0.0)),
                                    bottom=float(w.get("bottom", 0.0)),
                                ),
                            )
                        )
                except Exception:
                    words_ir = []

                # Extract reading-order text lines as TextBlock
                text_blocks: list[TextBlock] = []
                try:
                    if hasattr(page, "extract_text_lines"):
                        lines_raw = page.extract_text_lines() or []
                        for l in lines_raw:
                            line_text = str(l.get("text", "")).strip()
                            if not line_text:
                                continue
                            line_bbox = BBox(
                                x0=float(l.get("x0", 0.0)),
                                top=float(l.get("top", 0.0)),
                                x1=float(l.get("x1", 0.0)),
                                bottom=float(l.get("bottom", 0.0)),
                            )
                            text_blocks.append(
                                TextBlock(
                                    text=line_text,
                                    page_number=page_num,
                                    bbox=line_bbox,
                                )
                            )
                except Exception:
                    text_blocks = []

                # Fallback text blocks if extract_text_lines gave nothing but raw_text exists
                if not text_blocks and raw_text:
                    for line in raw_text.splitlines():
                        stripped = line.strip()
                        if stripped:
                            text_blocks.append(
                                TextBlock(
                                    text=stripped,
                                    page_number=page_num,
                                )
                            )

                # Extract tables
                # Use permissive settings so thin-border ReportLab tables
                # (0.3 pt lines, non-black colours) are detected.  The
                # defaults (snap_tolerance=3, join_tolerance=3) are sufficient
                # for standard PDFs; raising both to 4 makes the table finder
                # merge nearby thin line segments into table edges, exposing
                # the teal/grey summary tables produced by the synthetic
                # generators.  min_words_vertical/horizontal=0 prevents
                # pdfplumber from discarding edge segments that fall in
                # otherwise text-sparse cells (e.g. sparse summary rows).
                # NOTE: "min_words_in_text_strip" is NOT a valid pdfplumber
                # TableSettings field; use min_words_vertical / _horizontal.
                _table_settings = {
                    "snap_tolerance": 4,
                    "join_tolerance": 4,
                    "min_words_vertical": 0,
                    "min_words_horizontal": 0,
                }
                tables_ir: list[TableIR] = []
                try:
                    table_candidates = page.find_tables(table_settings=_table_settings) or []
                except Exception:
                    try:
                        table_candidates = page.find_tables() or []
                    except Exception:
                        table_candidates = []

                for t in table_candidates:
                    try:
                        t_bbox = None
                        if getattr(t, "bbox", None) is not None:
                            t_bbox = BBox(
                                x0=float(t.bbox[0]),
                                top=float(t.bbox[1]),
                                x1=float(t.bbox[2]),
                                bottom=float(t.bbox[3]),
                            )

                        extracted_grid = t.extract() or []
                        cleaned_rows: list[list[str]] = []
                        for row in extracted_grid:
                            cleaned_rows.append(
                                [(cell.strip() if cell is not None else "") for cell in row]
                            )

                        cells: list[TableCell] = []
                        if hasattr(t, "rows") and t.rows:
                            for r_idx, row_obj in enumerate(t.rows):
                                if not hasattr(row_obj, "cells"):
                                    continue
                                for c_idx, cell_bbox in enumerate(row_obj.cells):
                                    cell_txt = ""
                                    if r_idx < len(cleaned_rows) and c_idx < len(cleaned_rows[r_idx]):
                                        cell_txt = cleaned_rows[r_idx][c_idx]

                                    cb: BBox | None = None
                                    if cell_bbox is not None:
                                        cb = BBox(
                                            x0=float(cell_bbox[0]),
                                            top=float(cell_bbox[1]),
                                            x1=float(cell_bbox[2]),
                                            bottom=float(cell_bbox[3]),
                                        )

                                    cells.append(
                                        TableCell(
                                            text=cell_txt,
                                            row_index=r_idx,
                                            col_index=c_idx,
                                            bbox=cb,
                                        )
                                    )

                        tables_ir.append(
                            TableIR(
                                page_number=page_num,
                                bbox=t_bbox,
                                rows=cleaned_rows,
                                cells=cells,
                            )
                        )
                    except Exception:
                        # Skip this individual table; keep any already-extracted ones.
                        pass

                pages_ir.append(
                    PageIR(
                        page_number=page_num,
                        width=width,
                        height=height,
                        raw_text=raw_text,
                        text_blocks=text_blocks,
                        words=words_ir,
                        tables=tables_ir,
                    )
                )

        except Exception as err:
            raise PDFExtractionError(
                f"Error processing pages in PDF '{file_path}': {err}"
            ) from err

    return DocumentIR(
        source_path=str(file_path),
        page_count=len(pages_ir),
        pages=pages_ir,
        metadata=clean_metadata,
    )
