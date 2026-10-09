# Step 04: PDF Extraction → Intermediate Representation (IR)

## What Was Implemented

Implemented the `src/ir/` module providing a library-agnostic Intermediate Representation (IR) for PDF documents and a deterministic extractor API powered by `pdfplumber`.

### Sub-modules and Classes

| File / Component | Purpose |
|------------------|---------|
| `src/ir/models.py` | Pydantic IR data structures: `BBox`, `WordIR`, `TextBlock`, `TableCell`, `TableIR`, `PageIR`, `DocumentIR` |
| `src/ir/exceptions.py` | `PDFExtractionError` for corrupted, missing, empty, or un-decryptable PDF files |
| `src/ir/extractor.py` | `extract_pdf(path, password=None) -> DocumentIR` extracting pages, text lines, words with bounding boxes, and structured tables |
| `src/ir/__init__.py` | Public module interface re-exporting domain models, exceptions, and `extract_pdf` |
| `requirements.txt` | Dependency configuration recording `pydantic`, `reportlab`, `PyPDF2`, `pdfplumber`, and `pytest` |
| `tests/test_ir.py` | 12 focused pytest test cases covering extraction, tables, multi-page flows, bounding boxes, errors, and passwords |

## Files Created / Modified

**Created:**
- `src/ir/__init__.py`
- `src/ir/models.py`
- `src/ir/extractor.py`
- `src/ir/exceptions.py`
- `requirements.txt`
- `tests/test_ir.py`
- `docs/ai_log/step-04.md`

**Modified:**
- None (existing models, sample generator, and tests were untouched)

## Intermediate Representation (IR) Schema

1. **DocumentIR**:
   - `source_path: str | None`
   - `page_count: int`
   - `pages: list[PageIR]`
   - `metadata: dict[str, Any]`
   - Methods: `get_page(page_number)`, `get_all_text()`, `get_all_tables()`

2. **PageIR**:
   - `page_number: int` (1-indexed)
   - `width: float`, `height: float`
   - `raw_text: str`
   - `text_blocks: list[TextBlock]` (lines preserving reading order with spatial bounding boxes)
   - `words: list[WordIR]` (word tokens with bounding boxes)
   - `tables: list[TableIR]` (tables on the page; empty list if none)

3. **TableIR & TableCell**:
   - `page_number: int`, `bbox: BBox | None`
   - `rows: list[list[str]]` (matrix of cleaned text values)
   - `cells: list[TableCell]` (individual cell tokens with `row_index`, `col_index`, `bbox`)
   - Helpers: `get_headers()`, `get_data_rows()`, `row_count`, `col_count`

4. **BBox**:
   - `x0, top, x1, bottom` (PDF points measured from top-left)
   - Properties: `width`, `height`, `as_tuple()`

## Important Design Decisions

1. **Isolation of PDF extraction library (`pdfplumber`)**:
   Downstream components (classifier, fingerprinting, deterministic engine, LLM learner) consume `DocumentIR` exclusively and never import `pdfplumber` directly.

2. **Preservation of multiple levels of granularity**:
   - `raw_text`: fast string/regex search and LLM context prompts
   - `text_blocks`: line-level anchor detection and reading order
   - `words`: precise coordinate-based anchor matching and proximity locators
   - `tables` & `cells`: tabular grid parsing with cell-level bounds

3. **Deterministic Error Handling**:
   `extract_pdf` performs pre-checks (file existence, file type, non-zero file size) and wraps underlying extraction and encryption failures in a clear `PDFExtractionError`. Password-protected PDFs without a supplied password raise an informative error indicating that a password is required.

4. **Clean tabular cell extraction**:
   Tables extracted by `pdfplumber` are normalized so that `None` cells are cast to empty strings `""` and cell whitespace is trimmed, avoiding `NoneType` attribute errors in subsequent parser stages.

## Tests Run

Ran:
```
PYTHONPATH=. pytest -v
```

Output:
```
============================== 66 passed in 0.57s ==============================
```

Covering:
- Format A PDF extraction (`DocumentIR`, dimensions, page numbers)
- Text and word token extraction with bounding box checks
- Table extraction with headers, rows, cells, and bounding boxes
- Multi-page document extraction preserving 1-indexed page ordering
- Pages containing text but no tables (`tables == []`)
- Error conditions: non-existent file, directory path, 0-byte file, corrupt non-PDF file
- Password handling: encrypted PDF fails without password, succeeds with correct password
- Extraction of Format B (PMS layout variant) and Format C (AIF layout)

## Limitations

1. **Table detection relies on visible rules / lines**:
   `pdfplumber`'s default `find_tables()` detects tables that have ruling lines (explicit table borders). For completely borderless whitespace-delimited tables, custom table settings (`explicit_vertical_lines` or stream-based column boundaries) or text block layout analysis may be needed if encountered in real-world statements.
2. **Scanned image PDFs are not OCR'd**:
   `pdfplumber` extracts digital text and vector lines. Image-only scans without an embedded text layer will produce empty text blocks unless preprocessed with an OCR engine.
