# AI Implementation & Engineering Log

This document consolidates all chronological implementation logs, architectural decisions, bugs encountered, fixes applied, test verification outcomes, and operational boundaries across all phases of the **PMS & AIF Investment Statement Parser**.

---

## Table of Contents

- [Overview & Architecture Summary](#overview--architecture-summary)
- [Step 01: Initial Project Scaffolding](#step-01-initial-project-scaffolding)
- [Step 02: Core Domain Models & Status Enums (Build Step 1)](#step-02-core-domain-models--status-enums-build-step-1)
- [Step 03: Synthetic Sample Generator (Build Step 2)](#step-03-synthetic-sample-generator-build-step-2)
- [Step 04: PDF Extraction & Intermediate Representation (Build Step 3)](#step-04-pdf-extraction--intermediate-representation-build-step-3)
- [Step 05: Rule-Based Document Classifier (Build Step 4)](#step-05-rule-based-document-classifier-build-step-4)
- [Step 06: Format Fingerprinting & Layout Drift Detection (Build Step 5)](#step-06-format-fingerprinting--layout-drift-detection-build-step-5)
- [Step 07: Deterministic Parsing Specifications & SpecStore (Build Step 6)](#step-07-deterministic-parsing-specifications--specstore-build-step-6)
- [Step 08: Deterministic Parsing Engine (Build Step 7)](#step-08-deterministic-parsing-engine-build-step-7)
- [Step 09: Statement Financial Validator (Build Step 8)](#step-09-statement-financial-validator-build-step-8)
- [Step 10: Provider-Agnostic LLM Adapter & Audit Ledger (Build Step 9)](#step-10-provider-agnostic-llm-adapter--audit-ledger-build-step-9)
- [Step 11: PII Redactor for Training & Learning Prompts (Build Step 10)](#step-11-pii-redactor-for-training--learning-prompts-build-step-10)
- [Step 12: Format Learner & Human Feedback Adapter (Build Step 11)](#step-12-format-learner--human-feedback-adapter-build-step-11)
- [Step 13: Persistent SQLite Storage Layer (Build Step 12)](#step-13-persistent-sqlite-storage-layer-build-step-12)
- [Step 14: End-to-End Pipeline Integration (Build Step 13)](#step-14-end-to-end-pipeline-integration-build-step-13)
- [Step 15: Ingestion Subsystems — Folder, Email & Gmail IMAP (Build Step 14)](#step-15-ingestion-subsystems--folder-email--gmail-imap-build-step-14)
- [Step 16: Command-Line Interface (CLI) (Build Step 15)](#step-16-command-line-interface-cli-build-step-15)
- [Step 17: Parser Evaluation Framework & Ground-Truth Benchmarking (Build Step 16)](#step-17-parser-evaluation-framework--ground-truth-benchmarking-build-step-16)
  - [Step 17.2: Synthetic SpecStore Fixture Fix](#step-172-synthetic-specstore-fixture-fix)
- [Consolidated Known Limitations & Operational Boundaries](#consolidated-known-limitations--operational-boundaries)

---

## Overview & Architecture Summary

The PMS & AIF Investment Statement Parser is structured as a deterministic, template-driven ingestion, parsing, and financial validation system designed for Indian investment portfolios.

The processing lifecycle enforces strict separation of concerns across deterministic extraction and optional AI-assisted learning:

```
[Ingestion: Folder / Email / Gmail IMAP]
                 │
                 ▼
     [Deduplication: SHA-256]
                 │
                 ▼
 [DocumentIR Extractor: pdfplumber]  ──(password required?)──► NEEDS_PASSWORD
                 │
                 ▼
  [Classifier: Multi-Tiered Rules]   ──(score < 25)──────────► SKIPPED
                 │                   ──(25 <= score < 50)────► MAYBE (quarantined)
                 ▼ (STATEMENT: PMS / AIF)
 [Layout Fingerprinter: Anchors & Tables]
                 │
        ┌────────┴───────────────────────────┐
        ▼                                    ▼
Known Format (Score >= 0.90)       Suspected Drift (0.65 <= Score < 0.90)
        │                                    │
        │                                    ▼
        │                          [DRIFT_SUSPECTED]
        │                     (Quarantined - No Auto-Learner)
        ▼
[Deterministic Parsing Engine]
  - Anchor-relative locators
  - Tabular column mapping
        │
        ▼
[Financial Validator]
  - Cash flow reconciliation
  - Portfolio weight summation
  - ISIN Luhn checksums
  - NAV valuation consistency
        │
        ├────────────────────────────┐
        ▼                            ▼
Clean (PARSED)        Issues (PARSED_WITH_WARNINGS / NEEDS_REVIEW)
        │                            │
        └─────────────┬──────────────┘
                      ▼
        [StatementStore (SQLite WAL)]
```

When an unrecognized format (`UNKNOWN`, score < 0.65) is encountered:
1. If `--learn` is disabled: The document is quarantined with `NEEDS_REVIEW`.
2. If `--learn` is enabled: The `Learner` masks sensitive customer PII via `Redactor`, prompts an OpenAI-compatible LLM for candidate parsing rules, validates the candidate against the training document via `ParsingEngine` and `Validator`, and produces a draft proposal (`draft_*.json`). The draft remains quarantined until explicitly approved by an operator via the CLI (`--approve <DRAFT_ID>`).

---

## Step 01: Initial Project Scaffolding

- **Status / Origin**: Repository initialization and workspace setup.
- **Log Notes**: The initial step-wise log file (`docs/ai_log/step-01.md`) contained only the header `# Step 01` without operational body text. Repository structure, environment management (`.venv`), and Python package layout (`src/`, `tests/`, `docs/`) were established during this step.

---

## Step 02: Core Domain Models & Status Enums (Build Step 1)

### What Was Implemented
Implemented the foundational domain models and enumeration types for the PMS / AIF parser service using Pydantic (v2), strictly adhering to PMS Parser Project Plan v4:

1. **Enumerations (`src/models/enums.py`)**:
   - `StatementType`: `PMS`, `AIF` (case-insensitive string enumeration).
   - `TransactionType`: `buy`, `sell`, `dividend`, `fee`, `inflow`, `outflow` (case-insensitive string enumeration).
   - `DocumentStatus`: All 11 statuses defined in Plan v4:
     `DUPLICATE`, `NEEDS_PASSWORD`, `SKIPPED`, `MAYBE`, `CLASSIFIED`, `PARSED`, `PARSED_WITH_WARNINGS`, `NEEDS_REVIEW`, `DRIFT_SUSPECTED`, `LEARNING_FAILED`, `FAILED`.

2. **Domain Models (`src/models/models.py`)**:
   - `Holding`: Represents individual portfolio positions with `security_name`, `isin`, `quantity`, `cost`, `market_value`, `weight`. All monetary/quantitative values modeled as `Decimal`.
   - `Transaction`: Represents portfolio cash/security transactions with `date` (supports `datetime.date` or ISO string), `type` (`TransactionType`), `security`, `quantity` (`Decimal`), and signed cash impact `amount` (`Decimal`).
   - `Summary`: Portfolio valuation and cash metrics with all 10 summary fields (`opening_value`, `closing_value`, `opening_cash`, `closing_cash`, `fees_charged`, `income`, `realized_gains`, `unrealized_gains`, `units`, `nav_per_unit`) defaulting to `None`.
   - `Statement`: Top-level statement entity with metadata (`provider_name`, `strategy_name`, `client_identifier`, `statement_period`, `currency`, `statement_type`), `holdings` (defaults to empty list `[]`), `transactions` (defaults to empty list `[]`), and optional `summary`.
   - `ParseResult`: Container for the output of parsing, encapsulating `statement`, `holdings`, `transactions`, `summary`, `status` (defaults to `DocumentStatus.PARSED`), `warnings`, `errors`, and `raw_metadata`.

### Files Created / Modified
- `src/__init__.py`: Package marker for `src`.
- `src/models/__init__.py`: Re-exports domain models and enums.
- `src/models/enums.py`: Defines `StatementType`, `TransactionType`, and `DocumentStatus`.
- `src/models/models.py`: Defines `Holding`, `Transaction`, `Summary`, `Statement`, and `ParseResult`.
- `tests/test_models.py`: Focused test suite for models and enums.

### Design Decisions
1. **Decimal for Monetary and Quantitative Values**: All monetary amounts, quantities, NAVs, and weights use `Decimal` to avoid IEEE 754 floating-point inaccuracies.
2. **Case-Insensitive Enums**: Enums implement `_missing_` classmethods so string inputs like `"pms"`, `"PMS"`, `"buy"`, or `"BUY"` resolve to the canonical enum members.
3. **Empty List Default for Holdings**: `holdings` defaults to a factory-generated empty list `[]` rather than `None`. This supports AIFs (which naturally have 0 holdings) and allows PMS zero-holdings validation rules to check `len(holdings) == 0`.
4. **ParseResult as Simple Container**: `ParseResult` is kept as a simple, transparent container without implicit synchronization logic.
5. **Separation of Model Validation from Business Validation**: Models permit constructing partial and raw data representations, deferring comprehensive cross-field identity rules to the dedicated Validator module in Step 9.

### Test Verification
Executed test suite with pytest under Python 3.14 via `.venv`:
- `tests/test_models.py::test_model_creation` PASSED
- `tests/test_models.py::test_decimal_values` PASSED
- `tests/test_models.py::test_default_empty_holdings` PASSED
- `tests/test_models.py::test_enum_status_values` PASSED
- `tests/test_models.py::test_basic_pydantic_validation` PASSED
- `tests/test_models.py::test_json_roundtrip_and_precision` PASSED
- `tests/test_models.py::test_aif_structure` PASSED
- **Result**: 7 passed in 0.10s.

---

## Step 03: Synthetic Sample Generator (Build Step 2)

### What Was Implemented
Implemented the `sample_generator` module (`src/sample_generator/`) that generates synthetic PMS and AIF investment statement PDFs and matching ground-truth JSON files for deterministic testing of all later pipeline steps.

### Sub-modules
| File | Purpose |
|------|---------|
| `_isin.py` | ISIN generation with valid Luhn-variant check digits |
| `_data.py` | Canonical financial dataset definitions (6 variants); enforces cash identity and weight reconciliation at construction time |
| `_pdf.py` | Shared ReportLab styles, table helpers, formatting functions |
| `_ground_truth.py` | Serialises `SampleData` to ground-truth JSON (Decimal-safe) |
| `formats/format_a.py` | Format A — normal PMS PDF layout |
| `formats/format_a_messy.py` | Format A-messy — multi-page holdings, repeated headers, wrapped name |
| `formats/format_a_drift.py` | Format A-drift — mutated layout to trigger `DRIFT_SUSPECTED` |
| `formats/format_b.py` | Format B — completely different PMS layout (green theme, horizontal summary) |
| `formats/format_c.py` | Format C — AIF statement (Fund Overview, Cash Ledger, no holdings) |
| `generator.py` | `generate_all(out_dir)` — wires all builders, creates encrypted sample |
| `__init__.py` | Package init; exposes `generate_all` |
| `__main__.py` | `python -m src.sample_generator [--out DIR]` CLI |

### Files Created / Modified
- Created: `src/sample_generator/_isin.py`, `_data.py`, `_pdf.py`, `_ground_truth.py`, `generator.py`, `__init__.py`, `__main__.py`, format builders under `src/sample_generator/formats/`, `tests/test_sample_generator.py`.
- Modified: `.gitignore` — added `samples/synthetic/` (generated artifacts are not committed).

### Sample Variants Generated
| Label | Type | Pages | Special Features |
|-------|------|-------|-----------------|
| `format_a1` | PMS | 1 | 5 holdings, 7 transactions, full summary |
| `format_a2` | PMS | 1 | Different quarter, 4 holdings, 5 transactions |
| `format_b` | PMS | 1 | Green theme, horizontal summary, `YYYY-MM-DD` dates |
| `format_c` | AIF | 1 | Empty holdings, NAV per unit, units, Cash Ledger section |
| `format_a_messy` | PMS | 2 | 10 holdings spanning 2 pages, repeated table header, 1 wrapped security name |
| `format_a_drift` | PMS | 1 | Renamed title & summary heading, reordered+renamed columns, `DD-Mon-YYYY` dates |
| `format_a1_encrypted` | PMS | 1 | RC4-encrypted clone of `format_a1`; password in `generator.ENCRYPTED_SAMPLE_PASSWORD` |

### Financial Consistency Properties
Every dataset is verified at construction (Python `assert`) before writing:
- **Cash identity**: `closing_cash = opening_cash + Σ(signed amounts)` — enforced in `SummaryData.verify_cash_identity()`
- **AIF NAV identity**: `|units × nav_per_unit - closing_value| ≤ 1% × closing_value` — enforced in `SummaryData.verify_nav_identity()`
- **Holdings weights ≈ 100%**: final holding's weight is adjusted to make the sum exact
- **ISIN check digits**: `make_isin()` uses the Luhn-variant algorithm; `validate_isin()` verifies any ISIN
- **Sign convention**: inflow/sell/dividend → `+`; buy/fee/outflow → `−`

### Test Verification
Executed pytest: 54 passed (7 model tests + 47 new sample generator tests) in 0.38s.

---

## Step 04: PDF Extraction & Intermediate Representation (Build Step 3)

### What Was Implemented
Implemented the `src/ir/` module providing a library-agnostic Intermediate Representation (IR) for PDF documents and a deterministic extractor API powered by `pdfplumber`.

### Components
- `src/ir/models.py`: Pydantic IR data structures: `BBox`, `WordIR`, `TextBlock`, `TableCell`, `TableIR`, `PageIR`, `DocumentIR`.
- `src/ir/exceptions.py`: `PDFExtractionError` for corrupted, missing, empty, or un-decryptable PDF files.
- `src/ir/extractor.py`: `extract_pdf(path, password=None) -> DocumentIR` extracting pages, text lines, words with bounding boxes, and structured tables.
- `src/ir/__init__.py`: Public module interface.
- `requirements.txt`: Dependencies recorded (`pydantic`, `reportlab`, `PyPDF2`, `pdfplumber`, `pytest`).
- `tests/test_ir.py`: 12 focused pytest test cases.

### Important Design Decisions
1. **Isolation of PDF extraction library (`pdfplumber`)**: Downstream components (classifier, fingerprinting, deterministic engine, LLM learner) consume `DocumentIR` exclusively and never import `pdfplumber` directly.
2. **Preservation of multiple levels of granularity**:
   - `raw_text`: fast string/regex search and LLM context prompts
   - `text_blocks`: line-level anchor detection and reading order
   - `words`: precise coordinate-based anchor matching and proximity locators
   - `tables` & `cells`: tabular grid parsing with cell-level bounds
3. **Deterministic Error Handling**: Explicit pre-checks for file existence and 0-byte size; password-protected PDFs raise clean `PDFExtractionError`.
4. **Clean tabular cell extraction**: Tables extracted by `pdfplumber` normalize `None` cells to empty strings `""` and trim cell whitespace.

### Test Verification
66 passed in 0.57s (54 previous + 12 IR tests).

---

## Step 05: Rule-Based Document Classifier (Build Step 4)

### What Was Implemented
Implemented the deterministic rule-based document classifier in `src/classifier/` that evaluates an extracted `DocumentIR` and determines whether it is a financial statement (`STATEMENT`), an ambiguous document requiring human triage (`MAYBE`), or an irrelevant document (`SKIP`). Where evidence permits, it identifies whether the statement is `PMS` or `AIF`.

### Components
- `src/classifier/models.py`: `ClassificationDecision` enum (`STATEMENT`, `MAYBE`, `SKIP`) and `ClassificationResult` model.
- `src/classifier/rules.py`: Keyword dictionaries (`PMS_STRONG_PHRASES`, `AIF_STRONG_PHRASES`, `STATEMENT_ANCHORS`, `SUPPORTING_FINANCIAL_TERMS`, `IRRELEVANT_INDICATORS`).
- `src/classifier/classifier.py`: `classify_document(doc: DocumentIR) -> ClassificationResult`.
- `tests/test_classifier.py`: 10 unit test cases.

### Classification Logic & Scoring Design
1. **Safety Pre-checks**: 0 pages or whitespace-only text immediately returns `SKIP` with score 0.
2. **Negative Indicators (-35 pts each)**: Invoices, shipping notes, resumes, receipts heavily penalize the score.
3. **Multi-tiered Positive Signals**:
   - Strong Domain Phrases: +35 pts each (e.g. `"portfolio management statement"`, `"category ii aif"`).
   - Statement Anchors: +15 pts each, capped at 45 pts (e.g. `"statement period"`, `"client id"`).
   - Supporting Vocabulary: +5 to +20 pts max (e.g. `"holdings"`, `"isin"`). A single generic term cannot trigger `MAYBE` or `STATEMENT`.
   - Table Header Evidence: +15 to +35 pts max (e.g. `isin`, `qty`, `cost`, `market value`).
4. **Thresholds & Decision Mapping**:
   - `score >= 50`: `STATEMENT` (confidence `0.80`–`0.99`). Identified as `PMS` or `AIF`.
   - `25 <= score < 50`: `MAYBE` (confidence `0.40`–`0.70`). Routed to manual review queue.
   - `score < 25`: `SKIP` (confidence `0.00`–`0.30`). Rejected as non-statement.

### Test Verification
76 passed in 0.69s (66 previous + 10 classifier tests).

---

## Step 06: Format Fingerprinting & Layout Drift Detection (Build Step 5)

### What Was Implemented
Implemented `src/fingerprint/` for extracting structural layout feature sets from `DocumentIR` and comparing them using configurable similarity scoring. Rather than relying on rigid raw byte hashes, this module uses a feature-based representation capable of recognizing known templates, detecting subtle template mutations (drift), and categorizing unknown layouts.

### Components
- `src/fingerprint/models.py`: `FormatFingerprint`, `LayoutStats`, `FingerprintMatchStatus` (`KNOWN`, `DRIFT_SUSPECTED`, `UNKNOWN`), `FingerprintMatchResult`.
- `src/fingerprint/extractor.py`: `extract_fingerprint(doc, format_id=None)`.
- `src/fingerprint/comparator.py`: `compare_fingerprints(fp1, fp2, ...)` and `match_fingerprint(query_fp, library)`.
- `tests/test_fingerprint.py`: 9 focused test cases.

### Scoring Formula
- **Table Header Similarity (50% weight)**: Pairwise Jaccard vocabulary overlap combined with strict positional sequence matching.
- **Anchor Phrase Similarity (35% weight)**: Jaccard set overlap across structural anchors.
- **Layout Statistics Similarity (15% weight)**: Column count sequences and table count proximity.
- **Composite Score**: `0.50 * table_sim + 0.35 * anchor_sim + 0.15 * layout_sim`.

### Thresholds
- `similarity >= 0.90`: `KNOWN` (safe to parse with existing spec).
- `0.65 <= similarity < 0.90`: `DRIFT_SUSPECTED` (template mutation; triggers quarantine).
- `similarity < 0.65`: `UNKNOWN` (new format).

### Bugs Encountered & Fixes
Initial implementation failed 3 of 9 tests due to data-value contamination:
1. **Problem 1 (Anchor Pollution)**: Summary table lines like `"Opening Cash Balance ₹80,000.00"` were included as anchors, causing A1 vs A2 anchor similarity to drop to 0.48.
   - **Fix**: Implemented `_contains_data_value(norm)` rejecting digit tokens (`\d+`) and currency-prefixed amounts (`[a-z]\d+`).
2. **Problem 2 (Summary Table Header Contamination)**: 2-column summary tables (`["Opening Portfolio Value", "₹1,200,000.00"]`) were treated as structural tables.
   - **Fix**: Implemented `_is_structural_header_row()` requiring `>= 3` columns and rejecting any row containing data values.

### Empirical Scores After Fix
| Comparison Pair | Table Sim | Anchor Sim | Layout Sim | Composite | Status |
|---|---|---|---|---|---|
| A1 vs A2 | 1.0000 | 1.0000 | 1.0000 | **1.0000** | `KNOWN` |
| A1 vs Messy | 1.0000 | 1.0000 | 1.0000 | **1.0000** | `KNOWN` |
| A1 vs Drift | 0.7202 | 0.6667 | 1.0000 | **0.7435** | `DRIFT_SUSPECTED` |
| A1 vs B | 0.0000 | 0.0526 | 0.4333 | **0.0834** | `UNKNOWN` |
| A1 vs C | 0.0000 | 0.0000 | 0.1500 | **0.0225** | `UNKNOWN` |

### Test Verification
9 passed in `tests/test_fingerprint.py`.

---

## Step 07: Deterministic Parsing Specifications & SpecStore (Build Step 6)

### What Was Implemented
Implemented the deterministic parsing specification framework and `SpecStore` in `src/specs/` (with alias `src/spec/`):

1. **Typed Parsing Specification Models (`src/specs/models.py`)**:
   - `LocatorType`: `TABLE_AFTER_ANCHOR`, `VALUE_RIGHT_OF_ANCHOR`, `VALUE_BELOW_ANCHOR`, `VALUE_MATCHING_PATTERN`, `BOUNDING_BOX`.
   - `FieldLocator`: Pydantic model for scalar field locations; prioritizes anchor-relative positioning; supports `fallback_bbox`.
   - `TableLocator`: Pydantic model for structured tables (`header_keywords`, `column_mapping`, `stop_anchor`, `multi_page`, `repeat_headers`).
   - `ParsingSpec`: Top-level specification entity with lossless JSON serialization.
2. **Specification Exceptions (`src/specs/exceptions.py`)**:
   - `SpecError`, `SpecNotFoundError`, `SpecValidationError`, `DuplicateSpecError`.
3. **Filesystem SpecStore Repository (`src/specs/store.py`)**:
   - Manages specification files in `specs/<format_id>.json`.
   - Atomic writes via hidden temporary files and `os.replace`.
   - Fingerprint-based lookup: `get_by_fingerprint()` and `match_spec_by_fingerprint()`.
   - Path traversal guard rejecting invalid characters or directory escapes.

### Test Verification
24 passed in `tests/test_specs.py`.

---

## Step 08: Deterministic Parsing Engine (Build Step 7)

### What Was Implemented
Implemented the deterministic parsing engine in `src/engine/`. The engine accepts `DocumentIR` and `ParsingSpec` and returns a `ParseResult` populated with domain entities:

- `src/engine/converters.py`: Clean string parsing to `Decimal`, `datetime.date`, and `TransactionType`.
- `src/engine/locators.py`: Implements four locator strategies (`VALUE_RIGHT_OF_ANCHOR`, `VALUE_BELOW_ANCHOR`, `VALUE_MATCHING_PATTERN`, `BOUNDING_BOX`).
- `src/engine/table_parser.py`: Implements table discovery, header detection, column mapping resolution (numeric and fuzzy substring), data row iteration, and repeated header suppression (`_is_header_repeat()`).
- `src/engine/engine.py`: `ParsingEngine.parse(doc, spec) -> ParseResult` orchestrates metadata, summary, holdings, and transactions extraction.

### Architecture Decisions
- **Warnings-not-crashes**: Conversion errors and missing required fields record warnings and set the field to `None`; the engine does not raise unhandled exceptions during document extraction.
- **Layered locator search**: Searches tables -> text blocks -> spatial words in priority order.
- **Repeated header suppression**: Crucial for multi-page statements where headers repeat across page continuations.

### Test Verification
17 passed (14 engine tests + 3 converter tests) in `tests/test_engine.py`.

---

## Step 09: Statement Financial Validator (Build Step 8)

### What Was Implemented
Implemented a non-mutating, multi-check financial validator in `src/validator/`:

- `ValidatorConfig`: Configurable tolerances (`cash_tolerance=1.00`, `weights_tolerance_pct=0.50`, `nav_tolerance_pct=1.00`, `max_transaction_charge_pct=2.00`).
- **Eight Independent Checks (`src/validator/checks.py`)**:
  1. `check_identity_fields`: Completeness warnings (PMS zero holdings, missing client ID/provider).
  2. `check_cash_reconciliation`: `closing_cash = opening_cash + Σ(transaction.amount)` within tolerance.
  3. `check_nav_consistency`: `units × nav_per_unit ≈ closing_value` (AIF only).
  4. `check_portfolio_weights`: `Σ(holding.weight) ≈ 100%`.
  5. `check_quantity_price_value`: `quantity × price ≈ gross trade value / market value`.
  6. `check_transaction_signs`: Cash impact sign matches transaction type (buy/fee/outflow < 0; sell/inflow/dividend > 0).
  7. `check_isin_checksums`: Inline ISIN Luhn check digit verification.
  8. `check_transaction_dates`: Transaction dates fall within parsed statement period boundaries.
- **Status Mapping**:
  - Any `ERROR` finding -> `DocumentStatus.NEEDS_REVIEW`.
  - Only `WARNING` findings -> `DocumentStatus.PARSED_WITH_WARNINGS`.
  - Zero findings -> `DocumentStatus.PARSED`.

### Files Modified
- Added optional `price: Decimal | None = None` to `Holding` and `Transaction` in `src/models/models.py`.

### Test Verification
~74 test cases across 12 test classes passed in `tests/test_validator.py`.

---

## Step 10: Provider-Agnostic LLM Adapter & Audit Ledger (Build Step 9)

### What Was Implemented
Implemented a lightweight, provider-agnostic LLM adapter in `src/llm_adapter/` using Python's standard library (`urllib.request`) and Pydantic:

- `LLMConfig`: Endpoint URL, model name, credentials, timeout, replay mode, ledger path.
- `LLMAdapter`: Minimal `complete()` and `complete_request()` interface; validates untrusted outputs against Pydantic schemas or JSON Schema dicts; supports offline record and replay fixtures.
- `LLMLedger`: Tracks call count, token usage, latency, and estimated cost; appends structured audit lines to JSONL.
- `FakeLLMAdapter`: In-memory mock adapter returning canned JSON responses for offline testing.

### Security Guarantees
- API keys masked in `LLMConfig.__repr__` and `__str__`.
- Error messages never expose Authorization headers or secrets.
- Full PDF bytes and customer PII are strictly excluded from adapter requests.

### Test Verification
20 test scenarios across 8 test classes passed in `tests/test_llm_adapter.py`.

---

## Step 11: PII Redactor for Training & Learning Prompts (Build Step 10)

### What Was Implemented
Implemented a deterministic, in-memory PII redactor in `src/redactor/` that masks sensitive personal identifiers before sending text to an LLM:

- Supported Entity Types: `EMAIL`, `PHONE`, `PAN`, `AADHAAR`, `BANK_ACCOUNT`, `DEMAT`, `NAME`, `CUSTOM`.
- Guardrails: Strict boundary checks (`is_isin()`, `is_financial_number()`) ensure 12-character ISINs, dates, monetary values, and quantities are never corrupted.
- Consistent Masking: Standard mode (`[REDACTED_PAN]`) and indexed mode (`[REDACTED_EMAIL_1]`, `[REDACTED_EMAIL_2]`) guarantee repeated entities receive identical tokens.
- Privacy Guarantee: Plaintext sensitive values are discarded immediately after span replacement; metadata stores only offsets and truncated SHA-256 hashes.

### Test Verification
25 test cases across 13 test classes passed in `tests/test_redactor.py`.

---

## Step 12: Format Learner & Human Feedback Adapter (Build Step 11)

### What Was Implemented
Implemented the format learning and refinement orchestrator in `src/learner/`:

1. **Unknown Format Learning Loop**:
   - Sanitizes document text using `src/redactor`.
   - Prompts the LLM adapter to propose candidate `ParsingSpec` JSON.
   - Attaches the live document fingerprint.
   - Executes candidate spec on the source `DocumentIR` using `ParsingEngine`.
   - Validates extraction using `Validator`.
   - If validation passes and `auto_activate=True`: saves spec to `SpecStore` (`ACCEPTED`).
   - If validation passes and `auto_activate=False`: saves proposal to `draft_<format_id>.json` (`PROPOSED`).
   - If validation fails: feeds error findings into a repair prompt (up to 2 retries; 3 total attempts). If exhausted, marks `LEARNING_FAILED` without modifying `SpecStore`.
2. **Reviewer Feedback Adaptation (`learn_from_feedback`)**:
   - `feedback_validator.py`: Pre-validates human corrections (field whitelists, decimal conversions, arithmetic consistency) with zero LLM calls if invalid.
   - Re-runs extraction and checks reviewer corrections against extracted fields. Any discrepancy triggers the repair loop.
3. **Declared Locator Verification (`_verify_declared_locators`)**:
   - Validates that every locator declared in candidate specs resolves to actual values in the training document, preventing acceptance of hallucinated locators.

### Test Verification
14 test cases across 6 test classes passed in `tests/test_learner.py`.

---

## Step 13: Persistent SQLite Storage Layer (Build Step 12)

### What Was Implemented
Implemented the persistent storage subsystem in `src/store/` providing ACID-compliant persistence using SQLite in WAL mode:

### Schema Overview
- `documents`: Document ID, SHA-256 file hash, status, format ID, serialized fingerprint, warnings, errors, metadata, timestamps.
- `statements`: 1-to-1 with documents; stores exact `Statement` models serialized to JSON (lossless Decimal round-trip).
- `validation_results`: 1-to-N audit findings per document.
- `processing_attempts`: Multi-attempt execution logs tracking stage, status, and details.
- `llm_call_ledger`: Persistent token and cost audit records.

### Key Operations
- `StatementStore.create_document()`, `get_document_by_hash()`, `update_document_status()`.
- `save_parse_result()`: Atomic transaction updating document status, errors, warnings, and statement JSON.
- `export_statement_json(doc_id)`: Exports parsed statement JSON matching persistent storage.
- In-memory mode (`db_path=":memory:"`) retains persistent connection `_mem_conn` across transactions for testing.

### Test Verification
19 test cases across 7 test classes passed in `tests/test_store.py`.

---

## Step 14: End-to-End Pipeline Integration (Build Step 13)

### What Was Implemented
Implemented `Pipeline` in `src/pipeline/` unifying all subsystems into a non-interactive workflow:

1. Computes SHA-256 file hash; returns `DUPLICATE` immediately if content exists in store.
2. Extracts `DocumentIR` via `pdfplumber`; returns `NEEDS_PASSWORD` if encrypted and password missing/invalid.
3. Classifies document: `SKIP` ignored, `MAYBE` quarantined.
4. Matches fingerprint against `SpecStore`:
   - `DRIFT_SUSPECTED`: Quarantined immediately (no auto-learner).
   - `UNKNOWN`: If learner disabled -> `NEEDS_REVIEW`; if learner enabled -> executes learning loop.
   - `KNOWN`: Executes deterministic parsing engine.
5. Runs financial validator; assigns final status (`PARSED`, `PARSED_WITH_WARNINGS`, `NEEDS_REVIEW`).
6. Atomically saves results and attempt audit history in `StatementStore`.
7. Supports batch execution with fault isolation (one file crash never stops the batch).

### Post-Implementation Bug Fix (IR Text Fallback)
- **Problem**: In-memory test fixtures constructing `PageIR` with tables and words but leaving `raw_text=""` were short-circuiting at the classifier guard (`if not full_text.strip(): return SKIP`).
- **Fix**: Added multi-level text fallback in `src/ir/models.py`:
  1. `PageIR.raw_text` (if populated by pdfplumber).
  2. Joined word tokens (`WordIR.text`).
  3. Flattened table rows (`TableIR.rows`).
- `DocumentIR.get_all_text()` delegates to this fallback, ensuring in-memory fixtures and text-layer PDFs parse reliably.

### Test Verification
11 test cases across 8 test suites passed in `tests/test_pipeline.py`.

---

## Step 15: Ingestion Subsystems — Folder, Email & Gmail IMAP (Build Step 14)

### What Was Implemented
Implemented directory, email, and live mailbox ingestion in `src/ingestion/`:

1. **Folder Ingestion (`src/ingestion/folder.py`)**:
   - `FolderIngester`: Scans directory trees for `*.pdf` files with deterministic sorting (alphabetical).
   - Resolves symlinks to prevent duplicate processing.
   - Fault-isolated execution aggregating batch metrics.
2. **Email Ingestion (`src/ingestion/email.py`)**:
   - `EmailIngester`: Extracts attachments from `email.message.EmailMessage`, `Message`, raw `.eml` bytes, or `EmailAttachment` models.
   - Filters non-PDF attachments (`.txt`, `.png`, `.csv`) safely.
   - Safe temporary staging using `tempfile.NamedTemporaryFile` with guaranteed unlinking in `finally` blocks.
   - In-batch SHA-256 deduplication.
3. **Gmail IMAP Adapter (`src/ingestion/gmail.py`)**:
   - `GmailIngester`: Connects to `imap.gmail.com:993` over SSL.
   - Strictly read-only (`readonly=True`).
   - Password masked in `GmailIngestionConfig.__repr__`.
   - Delegates fetched MIME messages directly to `EmailIngester`.

### Test Verification
15 unit tests passed in `tests/test_email_ingestion.py`.

---

## Step 16: Command-Line Interface (CLI) (Build Step 15)

### What Was Implemented
Implemented the terminal entry point in `src/cli/` (`python -m src.cli`):

- **Capabilities**:
  - Single PDF processing: `python -m src.cli <FILE.PDF> [--password PWD] [--verbose]`.
  - Folder batch ingestion: `python -m src.cli --folder <DIR> [--recursive]`.
  - Gmail ingestion: `python -m src.cli --gmail [MAILBOX] [--limit N] [--search CRITERIA]`.
  - Draft spec review & promotion: `python -m src.cli --list-drafts` and `python -m src.cli --approve <DRAFT_ID>`.
  - Live integration verification: `python -m src.cli --verify-ollama` and `python -m src.cli --verify-gmail`.
- **Exit Code Policy**:
  - `0`: Complete success (clean parse, duplicate, or clean folder batch).
  - `1`: Processing or validation failure (unsupported extension, document failed, or folder batch had >= 1 failure).
  - `2`: CLI usage / argument syntax error.
- **Security**: Passwords passed via `--password` or environment variables are never printed to stdout/stderr.

### Test Verification
15 unit tests passed in `tests/test_cli.py`.

---

## Step 17: Parser Evaluation Framework & Ground-Truth Benchmarking (Build Step 16)

### What Was Implemented
Implemented the evaluation framework in `src/eval/` to benchmark extraction accuracy against ground truth without database pollution (`store_results=False`):

- `EvaluationCase`: Holds expected fields, statement type, and sample path.
- `comparator.py`: Conservative field extraction, Decimal tolerance comparisons (`abs(actual - expected) <= tolerance`), date normalizations (`YYYY-MM-DD`).
- Match Statuses: `CORRECT`, `INCORRECT`, `MISSING`, `UNEXPECTED`. Missing fields never match legitimate zero or false values.
- Metrics Aggregated: Total cases, processed cases, successful cases, perfect cases (100% field match), overall field accuracy, breakdown by statement type (`PMS` vs `AIF`).
- Reporting: Human-readable console text reports and structured JSON exports (`python -m src.eval <DIR> --json-out report.json`).

### Step 17.2: Synthetic SpecStore Fixture Fix
- **Problem**: When running evaluation against synthetic test samples, the production `specs/` directory was empty, causing all documents to be marked `NEEDS_REVIEW` and all 114 expected fields to be reported as `MISSING`.
- **Fix**: Added `src/eval/synthetic_specs.py` (`build_synthetic_spec_store`):
  - Defines `ParsingSpec` objects for Format A, A-messy, A-drift, B, and C using exact locators.
  - Extracts live fingerprints from companion synthetic PDFs.
  - Registers specs into an isolated temporary `SpecStore` for the duration of the evaluation run.
  - Ensures the production `specs/` directory is never modified during evaluation runs.

### Test Verification
15 evaluation unit tests (`tests/test_evaluation.py`) + 13 synthetic spec tests (`tests/test_eval_synthetic_specs.py`) passed.

---

## Consolidated Known Limitations & Operational Boundaries

1. **Digital Vector PDFs Only**:
   - The extraction engine (`pdfplumber`) operates on vector-based digital PDFs with embedded text layers.
   - Purely scanned, photographed, or rasterized PDFs without text layers require upstream OCR pre-processing before ingestion.
2. **Deterministic Drift Quarantine**:
   - Suspected template drift (`0.65 <= similarity < 0.90`, `DRIFT_SUSPECTED`) is strictly quarantined.
   - The system never automatically updates or mutates active production specifications when drift is suspected; human approval via CLI is required.
3. **Spec Learning Isolation**:
   - When `--learn` is enabled for unknown formats, the LLM learner creates *draft* specifications (`specs/draft_*.json`).
   - Draft specifications remain inactive until explicitly inspected and activated via `python -m src.cli --approve <DRAFT_ID>`.
4. **Specification Seeding**:
   - Approved parsing specifications for synthetic demo formats (A, B, C) are generated with live fingerprints extracted from synthetic PDFs. Run `PYTHONPATH=. .venv/bin/python scripts/bootstrap_specs.py` once after generating samples to seed `specs/`.
5. **JSON Export (Future Enhancement)**:
   - Direct CLI export of parsed statements to standalone JSON files is out of scope for the current release. Extracted statements and validation findings are persisted in the SQLite database (`data/pms_parser.db`) and queryable via `StatementStore`.
6. **Local SQLite Concurrency**:
   - `StatementStore` uses SQLite with WAL mode, optimized for single-host sequential or light concurrent processing. High-throughput distributed ingestion across multiple worker nodes would require migrating to an enterprise database (e.g. PostgreSQL).
7. **Read-Only Email Ingestion**:
   - Gmail IMAP ingestion connects strictly in read-only mode (`readonly=True`). The parser does not alter mailbox flags, mark messages as read, delete emails, or transmit outgoing messages.
8. **Currency & Language Scope**:
   - Keyword rules, date parsing heuristics, and currency symbols (`INR`, `₹`) are tailored for Indian PMS and AIF investment statements written in English. Multi-currency cross-rate conversions within a single statement are not performed.


### Where AI helped most

- **Breaking the project into implementable steps.** AI helped turn the PMS Parser plan into modules covering document ingestion, the intermediate representation (IR), classification, parsing, validation, learning, and SQLite storage. This made a fairly large project easier to implement and review incrementally.
- **Debugging across modules.** It helped trace why documents were being classified as `SKIPPED` when the page text existed in words or table cells but not in `raw_text`. Fixing the text fallback in `PageIR.get_text()` resolved the issue.
- **Building the learning workflow.** AI helped implement the LLM adapter, sensitive-data redaction, draft specification generation, validation, and the explicit approval workflow for unknown formats.
- **Working within limited tool access.** When Antigravity usage became limited, I used chat to review implementation steps and prepare focused changes, then ran commands and tests locally.

### Where AI misled me, and what I changed

- **Assuming JSON export existed.** AI initially directed me to look for exported JSON without first establishing whether the CLI actually wrote it. I had to inspect the pipeline’s export logic and distinguish parsed data stored in SQLite from JSON files on disk.
- **Assuming a successful integration check proves the whole pipeline.** The live Ollama check confirmed that the local model could respond with structured output, but that alone did not prove that an arbitrary real PDF could be parsed correctly. I treated model connectivity, PDF parsing, validation, and output persistence as separate things to verify.

Overall, AI was most useful as an implementation and debugging assistant, but it sometimes filled gaps in its knowledge with plausible assumptions. I learned to verify filenames, inspect the actual code and CLI, and run tests and integration checks myself before treating a feature as complete.
