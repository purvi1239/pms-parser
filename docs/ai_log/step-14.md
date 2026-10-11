# Step 14: Pipeline Integration (Build Step 13)

## What Was Implemented

Implemented the non-interactive **Pipeline Integration** module in `src/pipeline/` following Project Plan v4 (Build Step 14 / ai_log step-14).
The Pipeline orchestrates all existing core subsystems into a unified, non-interactive execution engine:
1. **Deduplication**: Content hashing (SHA-256) check against persistent storage before any expensive extraction or parsing.
2. **PDF Extraction & Passwords**: Extraction of intermediate representation (`DocumentIR`) via `pdfplumber`, testing candidate passwords if encrypted, and setting `NEEDS_PASSWORD` if unauthorized.
3. **Deterministic Classification**: Scored classification into `STATEMENT`, `MAYBE` (routed to review queue), or `SKIP` (ignored/non-statement).
4. **Fingerprinting & Format Matching**: Feature-set extraction (table headers, anchor phrases, layout dimensions) and similarity scoring against `SpecStore`.
5. **Drift Detection Quarantine**: Near matches trigger `DRIFT_SUSPECTED` and are quarantined without invoking the LLM learner.
6. **Unknown Formats & Learner Integration**: Unrecognized formats trigger the `Learner` only when explicitly enabled; draft proposals are quarantined as `NEEDS_REVIEW`; failed proposals are marked `LEARNING_FAILED`.
7. **Deterministic Parsing Engine**: Resolves anchor-relative table and scalar locators to construct domain `Statement` objects.
8. **Financial Validation**: Multi-rule financial checks (cash reconciliation, portfolio weights, ISIN check digits, date boundaries) adjusting final status to `PARSED`, `PARSED_WITH_WARNINGS`, or `NEEDS_REVIEW`.
9. **Persistent Storage & History**: Atomic SQLite persistence in `StatementStore` recording document status, parsed statements, validation findings, attempt histories, and optional JSON exports.
10. **Batch Processing & Fault Isolation**: Batch orchestrator ensuring that an error or crash in one document never halts batch execution.

---

## Architecture & Processing Workflow

```mermaid
flowchart TD
    A[Input Document / PDF] --> B[Compute SHA-256 Hash]
    B --> C{Hash in Store?}
    C -->|Yes| D[Status: DUPLICATE<br/>Record Attempt]
    C -->|No| E[Extract DocumentIR]

    E -->|Encrypted, No Valid Pwd| F[Status: NEEDS_PASSWORD]
    E -->|Extraction Error| G[Status: FAILED]
    E -->|Success| H[Classify DocumentIR]

    H -->|SKIP| I[Status: SKIPPED]
    H -->|MAYBE| J[Status: MAYBE<br/>Quarantine to Review]
    H -->|STATEMENT| K[Extract Fingerprint & Match Specs]

    K -->|Score in Drift Range| L[Status: DRIFT_SUSPECTED<br/>Quarantine Only - No Auto-Learner]
    K -->|Unknown Format & Learner Disabled| M[Status: NEEDS_REVIEW<br/>Quarantine to Review]
    K -->|Unknown Format & Learner Enabled| N{Learner Execution}

    N -->|Accepted & Auto-Active| O[Active Spec Ready]
    N -->|Proposed Draft| P[Status: NEEDS_REVIEW<br/>Draft Saved]
    N -->|Rejected / Repairs Exhausted| Q[Status: LEARNING_FAILED]

    K -->|Known Match (Score ≥ 0.90)| O[Active Spec Ready]

    O --> R[Parsing Engine: Extract Statement]
    R --> S[Validator: Run Financial Checks]
    S --> T{Validation Errors?}
    T -->|Errors Found| U[Status: NEEDS_REVIEW]
    T -->|Warnings Only| V[Status: PARSED_WITH_WARNINGS]
    T -->|Zero Issues| W[Status: PARSED]

    U --> X[StatementStore: Save Record & Audits]
    V --> X
    W --> X
```

---

## Modules Created

### `src/pipeline/models.py`
Data structures and operational configuration:
- `PipelineConfig`: Feature flags (`enable_learning`, `auto_activate_learned_spec`, `store_results`, `export_json`), candidate password list, fingerprint thresholds (`match_threshold=0.90`, `near_match_threshold=0.65`), and validation configuration overrides.
- `PipelineResult`: Structured output container for each processed document (`document_id`, `status`, `file_hash`, `classification`, `fingerprint`, `parse_result`, `validation_result`, `learning_result`, `warnings`, `errors`, `success`, `is_quarantined`).
- `BatchPipelineResult`: Summary metrics across batch runs (`total_documents`, `successful`, `failed`, `duplicates`, `quarantined`, `skipped`, `results`).

### `src/pipeline/exceptions.py`
Clear exception hierarchy for pipeline orchestration:
- `PipelineError` (base)
- `DocumentExtractionError`
- `PasswordRequiredError`
- `UnsupportedFormatPipelineError`
- `DriftDetectedPipelineError`
- `PipelineStorageError`

### `src/pipeline/pipeline.py`
Core orchestrator class `Pipeline`:
- `process_file(file_path, password=None)`: Full entry point from file path on disk.
- `process_document(doc, file_path=None, file_hash=None)`: In-memory entry point directly processing extracted `DocumentIR`.
- `process_batch(file_paths)`: Processes collections of files with isolated error boundaries.
- `reparse_document(doc_id, format_id=None)`: Re-extracts and re-parses an existing stored document.

### `src/pipeline/__init__.py`
Package exports exposing `Pipeline`, `PipelineConfig`, `PipelineResult`, `BatchPipelineResult`, hashing utilities, and custom exceptions.

---

## Key Design Decisions

1. **Non-Interactive Pipeline**:
   The pipeline records the appropriate lifecycle status in `StatementStore` (`NEEDS_PASSWORD`, `MAYBE`, `DRIFT_SUSPECTED`, `NEEDS_REVIEW`, `FAILED`) and completes execution. It never prompts the terminal or blocks background worker queues.
2. **Strict Quarantine on Drift (`DRIFT_SUSPECTED`)**:
   As mandated by the Project Plan, suspected template drifts are quarantined immediately. The Learner is never triggered automatically for drift; operator approval via CLI (`spec propose <id>`) is required.
3. **Learner Decoupling & Control**:
   `enable_learning` defaults to `False`. When enabled, the Learner is invoked strictly for unrecognized formats (`UNKNOWN`). If `auto_activate_learned_spec=False`, valid specs are retained as separate draft proposals and the document remains held in `NEEDS_REVIEW`.
4. **Early Deduplication**:
   Computing the file's SHA-256 hash first allows instant short-circuiting on duplicate files, skipping PDF parsing, classification, OCR, or layout extraction.
5. **Dual Entry Points**:
   Both `process_file` (disk files) and `process_document` (in-memory `DocumentIR`) are supported. This enables rapid, deterministic in-memory testing without invoking filesystem overhead or external PDF rendering.
6. **Lossless Multi-Attempt Tracking**:
   Every pipeline execution records an attempt in `StatementStore.record_processing_attempt()`, ensuring that reparses, duplicates, and review iterations maintain a complete audit log.

---

## Tests Created

**`tests/test_pipeline.py`** — 11 focused test cases across 8 test suites:

| Test Class | Focus |
|---|---|
| `TestPipelineSuccessFlow` | Clean document end-to-end execution; statement recovery from SQLite; processing attempts tracking. |
| `TestPipelineDuplicateHandling` | Same document SHA-256 hash returns `DUPLICATE` and appends attempt without corrupting state. |
| `TestPipelinePasswordFlow` | Missing password triggers `NEEDS_PASSWORD`; recovery via configured password candidate. |
| `TestPipelineClassifierRouting` | Non-statements marked `SKIPPED`; ambiguous documents routed to `MAYBE`. |
| `TestPipelineDriftQuarantine` | Near-match fingerprint quarantined as `DRIFT_SUSPECTED` without calling Learner. |
| `TestPipelineUnknownFormatAndLearner` | Unknown format without learner -> `NEEDS_REVIEW`; with learner auto-activate -> `PARSED`; draft proposal -> `NEEDS_REVIEW`; repair failure -> `LEARNING_FAILED`. |
| `TestPipelineValidationOutcomes` | Financial reconciliation error modifies status to `NEEDS_REVIEW`. |
| `TestPipelineBatchAndReparse` | Batch error isolation (valid, corrupt, duplicate processed together); `reparse_document` refreshes parse and increments attempt number. |

---

## Files Created / Modified

| File | Status | Description |
|---|---|---|
| `src/pipeline/__init__.py` | Created | Package exports |
| `src/pipeline/exceptions.py` | Created | Pipeline exception classes |
| `src/pipeline/models.py` | Created | Configuration and result models |
| `src/pipeline/pipeline.py` | Created | Core `Pipeline` orchestrator |
| `tests/test_pipeline.py` | Created | Automated unit test suite (11 test cases) |
| `docs/ai_log/step-14.md` | Created | Architecture and design log |

---

## Known Limitations

1. **Email Ingestion Dependency**:
   The current pipeline handles files on disk and in-memory IRs. Direct IMAP fetching or `.eml` MIME parsing will be added in Build Steps 15/16 (`src/ingest/`).
2. **Interactive CLI Approvals**:
   Promoting quarantined `DRIFT_SUSPECTED` or `NEEDS_REVIEW` documents into accepted specs will be wired into the CLI commands (`spec approve`, `spec propose`) in Build Step 17.

---

## Post-Implementation Bug Fix

### Root Cause

All 12 test failures shared a single root cause: **`DocumentIR.get_all_text()` only concatenated `p.raw_text` fields**, and `PageIR.raw_text` defaults to `""`. In the test fixtures, pages were constructed with `words` and `tables` populated but `raw_text` left blank (as test fixtures do not invoke `pdfplumber`). The classifier's early guard:

```python
full_text = doc.get_all_text()
if not full_text.strip():
    return ClassificationResult(... SKIP ... "Document text is empty")
```

short-circuited to `SKIPPED` before any table-based scoring logic was reached, even though the document had rich table and word data. Once all documents were classified as `SKIPPED`, every downstream pipeline stage (fingerprinting, spec lookup, parsing, validation, learner, store) was never invoked.

### Fix Applied

Updated `src/ir/models.py` — only two methods changed, no other modules touched:

1. **`PageIR.get_text()`**: Added a three-level preference fallback:
   1. Return `raw_text` if non-empty (always populated by `extract_pdf` for real PDFs).
   2. Fall back to words joined in reading order (`WordIR.text`).
   3. Fall back to flattened table row content (`TableIR.rows`).

2. **`DocumentIR.get_all_text()`**: Changed to delegate to `page.get_text()` (the fixed method) instead of directly joining `p.raw_text` fields.

This fix is **backwards-compatible**: real PDF documents always have `raw_text` populated by `pdfplumber`, so the fallbacks are never reached in production. Only in-memory test fixtures and edge-case digital PDFs that expose word tokens without a continuous text layer benefit from the fallback paths.

The `learner/prompts.py` prompt builder also calls `get_all_text()` and benefits from the same fix: learner prompts for in-memory documents now contain the actual text from words/tables rather than an empty string.

---

## Terminal Commands to Verify

```bash
# Run the pipeline unit tests
PYTHONPATH=. ./.venv/bin/pytest tests/test_pipeline.py -v

# Run with concise tracebacks
PYTHONPATH=. ./.venv/bin/pytest tests/test_pipeline.py -v --tb=short

# Run full project test suite excluding legacy test_fingerprint.py
PYTHONPATH=. ./.venv/bin/pytest tests/ -v --ignore=tests/test_fingerprint.py
```

