# Step 08: Deterministic Parsing Engine (Build Step 7)

## What Was Implemented

Implemented the deterministic parsing engine in `src/engine/`, strictly following Project Plan v4 and the Step 7 requirements. The engine accepts a `DocumentIR` (from Step 3/IR) and a `ParsingSpec` (from Step 6/Specs) and produces a `ParseResult` populated with domain models (`Statement`, `Holding`, `Transaction`, `Summary`) from `src/models/`.

No LLM calls, learning loops, or pipeline orchestration were added.

---

## Modules Created

### `src/engine/converters.py`

Type conversion utilities for extracted raw string values into domain types:

| Function | Description |
|---|---|
| `clean_text(val)` | Strips whitespace; returns `None` on empty strings |
| `to_decimal(val)` | Parses monetary/quantitative strings into `Decimal`. Handles `₹`, `INR`, `$`, `€`, commas, `%`, accounting parentheses `(1,234.50)`, and signed negatives. Raises `ValueError` on invalid non-numeric strings so the engine can record a warning. Returns `None` for explicit sentinel values (`"-"`, `"N/A"`, `"nil"`, etc.). |
| `to_date(val)` | Parses dates in 9 common formats (DD/MM/YYYY, ISO 8601, DD Mon YYYY, etc.). Returns the raw string rather than crashing when a format is unrecognised. |
| `to_transaction_type(val)` | Maps activity strings (buy, sell, subscription, redemption, div payout, fees, inflow, withdrawal, …) to `TransactionType` enum. Returns `None` for unrecognised values. |

---

### `src/engine/locators.py`

Implements the four field locator strategies:

#### `VALUE_RIGHT_OF_ANCHOR`
1. Searches all `TableIR` rows on the target page for a cell matching the anchor (case-insensitive, trailing colon-stripped). Returns the adjacent right cell.
2. Falls back to `TextBlock` text, scanning for anchor text within lines and extracting the substring after the anchor (and any `:`).
3. Finally searches spatial `WordIR` tokens: locates the anchor word sequence and collects words on the same baseline within `offset_x` points to the right.

#### `VALUE_BELOW_ANCHOR`
1. Searches `TableIR` rows for the anchor cell and returns the cell directly below it (next row, same column).
2. Falls back to spatial `WordIR` tokens: locates the anchor sequence, then collects words within `offset_y` points below and horizontally aligned.
3. Scans `raw_text` lines as a last resort (line immediately following the anchor).

#### `VALUE_MATCHING_PATTERN`
1. Compiles the regex and searches `PageIR.raw_text`.
2. Falls back to searching each `TextBlock` individually.
3. Returns `group(1)` if the pattern has a capture group; otherwise returns the full match.

#### `BOUNDING_BOX`
Extracts all `WordIR` tokens spatially overlapping the given bounding box (with 2pt margin) from the target page, sorted in reading order (row-then-left-to-right).

**Fallback behaviour**: If any anchor-relative strategy finds nothing and `FieldLocator.fallback_bbox` is set, the engine extracts text from that region and records a warning.

**Page-number filtering**: When `FieldLocator.page_number` is set, only that page is searched; otherwise all pages are tried in order and the first match is returned.

---

### `src/engine/table_parser.py`

Implements `TABLE_AFTER_ANCHOR` table extraction via `extract_raw_table_rows()`.

#### Table Discovery (`_find_candidate_tables`)
1. Searches `DocumentIR.pages` for the page whose `raw_text` contains the anchor phrase (case-insensitive normalised).
2. On that page, scores each `TableIR` by how many `header_keywords` appear in its first two rows; picks the highest-scoring table (or the first table on the page if none score).
3. If `multi_page=True`, continues onto subsequent pages collecting `TableIR`s whose column count is within ±1 of the initial table's, or whose first row repeats the headers.

#### Header Detection (`_find_header_row_index`)
Checks the first 3 rows of the table and returns the row index with the highest number of `header_keywords` keyword matches. Defaults to row 0 if no match.

#### Column Mapping (`_resolve_column_indices`)
Maps `column_mapping` keys (model field names) to 0-based column indices:
1. Numeric string values (e.g. `"0"`, `"3"`) are used directly.
2. Exact normalised string match against column headers.
3. Substring match (either direction) as a fuzzy fallback.

#### Data Row Iteration
- Empty rows are skipped.
- If `stop_anchor` is set, iteration terminates when any cell in a row contains the stop phrase.
- Repeated header rows (as detected by `_is_header_repeat`) are suppressed — crucial for multi-page tables with `repeatRows=1` in the PDF generator.

#### `parse_holdings_table` / `parse_transactions_table`
Higher-level wrappers that call `extract_raw_table_rows` and convert raw string dictionaries into typed `Holding` / `Transaction` instances, recording `ValueError` warnings from decimal/date conversions. Known total/subtotal rows are skipped. Unrecognised transaction types default to `BUY` with a warning.

---

### `src/engine/engine.py`

`ParsingEngine.parse(doc, spec) → ParseResult` orchestrates the full pipeline:

1. **Metadata extraction**: Iterates `spec.metadata_fields`, calls `extract_field_value`, applies `clean_text`. Missing required fields record a warning (no crash).
2. **Summary extraction**: Iterates `spec.summary_fields`, calls `extract_field_value`, converts values with `to_decimal`. Malformed defaults or extracted values set the field to `None` and record a warning.
3. **Holdings extraction**: Delegates to `parse_holdings_table` if `spec.holdings_table` is configured.
4. **Transactions extraction**: Delegates to `parse_transactions_table` if `spec.transactions_table` is configured.
5. **Statement assembly**: Constructs `Statement` from extracted metadata and attaches holdings, transactions, and the optional `Summary`.
6. **Status determination**: Sets `DocumentStatus.PARSED` if no warnings were generated; `DocumentStatus.PARSED_WITH_WARNINGS` otherwise.

`parse_document(doc, spec)` is a convenience function that creates a `ParsingEngine` and calls `parse()`.

---

## Tests Added

**`tests/test_engine.py`** — 14 focused unit tests using in-memory `DocumentIR` fixtures (no PDF I/O required, fast execution):

| Test | Covers |
|---|---|
| `test_value_right_of_anchor_from_table` | `VALUE_RIGHT_OF_ANCHOR` extraction from `TableIR` cells |
| `test_value_right_of_anchor_from_text` | `VALUE_RIGHT_OF_ANCHOR` extraction from `TextBlock` inline text |
| `test_value_below_anchor_from_table` | `VALUE_BELOW_ANCHOR` extraction from `TableIR` rows |
| `test_value_below_anchor_from_text` | `VALUE_BELOW_ANCHOR` extraction from spatial `WordIR` tokens |
| `test_value_matching_pattern` | `VALUE_MATCHING_PATTERN` regex extraction from raw text |
| `test_bounding_box_locator` | `BOUNDING_BOX` spatial extraction |
| `test_fallback_bbox_on_missing_anchor` | `fallback_bbox` triggers and records warning |
| `test_table_extraction_basic` | `TABLE_AFTER_ANCHOR` basic column mapping |
| `test_table_stop_anchor` | `stop_anchor` terminates row iteration correctly |
| `test_table_multi_page_repeated_headers` | Multi-page tables with repeated header rows suppressed |
| `test_full_parse_result` | End-to-end `parse_document` producing `ParseResult` |
| `test_missing_optional_fields` | Optional fields absent → `None`, no crash |
| `test_malformed_decimal_in_holdings` | Bad decimal values → warning recorded, field `None` |
| `test_page_number_constraint` | `page_number` filtering on `FieldLocator` |
| `test_decimal_conversion_formats` | `to_decimal` format coverage |
| `test_date_conversion_formats` | `to_date` format coverage |
| `test_transaction_type_conversion` | `to_transaction_type` alias coverage |

All 14 engine-specific tests pass. Converter unit tests (3 additional) also pass.

---

## Files Created

| File | Role |
|---|---|
| `src/engine/__init__.py` | Package exports |
| `src/engine/converters.py` | Type conversion utilities |
| `src/engine/locators.py` | Field locator strategies |
| `src/engine/table_parser.py` | Table extraction and domain model construction |
| `src/engine/engine.py` | `ParsingEngine` and `parse_document` |
| `tests/test_engine.py` | Focused unit tests |
| `docs/ai_log/step-08.md` | This document |

**No existing modules were modified.**

---

## Architecture Decisions

1. **Warnings-not-crashes**: Every `ValueError` from type conversion, every missing required field, and every fallback bbox activation generates a warning and sets the relevant domain field to `None`. The engine never raises unless there is a programming error.
2. **Layered locator search**: `VALUE_RIGHT_OF_ANCHOR` tries tables → text blocks → spatial words in priority order. This keeps the logic simple and predictable: structured table extraction is most reliable, free-text scanning is the fallback.
3. **Fuzzy header matching**: `_normalize()` strips punctuation and lowercases before comparing. Column mapping supports both numeric indices (explicit) and string label matching (fuzzy substring), making the engine tolerant of minor PDF extraction artefacts (extra spaces, missing parentheses).
4. **Repeated header suppression**: `_is_header_repeat()` counts keyword matches in a row. If ≥2 header keywords (or all keywords if fewer than 2 exist) are found in a data row, it is skipped. This handles `repeatRows=1` tables that echo the header on every continuation page.
5. **Determinism**: All operations are pure string matching, regex, and arithmetic. No randomness, no ML, no I/O beyond what `DocumentIR` already contains.

---

## Known Limitations

1. **Text-layer dependency**: The engine operates entirely on text extracted by pdfplumber. Scanned/image-only PDFs or heavily redacted documents return no text and will produce empty `ParseResult`s with missing-field warnings. OCR is outside scope.
2. **English-only conventions**: Currency aliases (`INR`, `USD`, etc.), transaction type mappings, and date format assumptions are English-centric.
3. **Linear text order assumed**: `VALUE_BELOW_ANCHOR` via text lines uses the raw line sequence from `raw_text`, which may not always correspond to visual vertical position in multi-column layouts.
4. **No fuzzy anchor matching**: Anchors must match exactly (case-insensitive, ignoring trailing colons). Misspellings or OCR noise will cause misses rather than near-matches.
5. **Column count heuristic for multi-page**: Continuation pages are matched by column-count proximity (±1). If a document contains two tables with similar column counts on the same page, the wrong table could be selected.
6. **No PDF coordinate awareness in table parser**: `TableIR` rows are plain string arrays without bounding boxes; the engine cannot verify a table's vertical position relative to a section anchor beyond page-level anchoring.
7. **No validation step**: `ParseResult` is not validated against ground truth or schema constraints in this step — that is reserved for Step 9 (Validation Engine).
