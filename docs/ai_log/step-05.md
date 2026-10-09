# Step 05: Document Classifier

## What Was Implemented

Implemented the deterministic rule-based document classifier in `src/classifier/` that evaluates an extracted `DocumentIR` and determines whether it is a financial statement (`STATEMENT`), an ambiguous document requiring human triage (`MAYBE`), or an irrelevant document (`SKIP`). Where evidence permits, it identifies whether the statement is `PMS` or `AIF`.

### Components Created

| File / Component | Purpose |
|------------------|---------|
| `src/classifier/models.py` | `ClassificationDecision` enum (`STATEMENT`, `MAYBE`, `SKIP`) and `ClassificationResult` model (decision, statement_type, confidence, score, reasons, signals) |
| `src/classifier/rules.py` | Categorized rule keywords and phrases: `PMS_STRONG_PHRASES`, `AIF_STRONG_PHRASES`, `STATEMENT_ANCHORS`, `SUPPORTING_FINANCIAL_TERMS`, `IRRELEVANT_INDICATORS`, and table header keywords |
| `src/classifier/classifier.py` | Core classification engine: `classify_document(doc: DocumentIR) -> ClassificationResult` evaluating multi-tiered text signals, table header layouts, and negative indicators |
| `src/classifier/__init__.py` | Module interface re-exporting `classify_document`, `ClassificationDecision`, and `ClassificationResult` |
| `tests/test_classifier.py` | 10 unit test cases testing PMS formats (A, B, A-messy, A-drift), AIF format (C), negative indicators, single generic terms, ambiguous documents, and empty documents |

## Files Created / Modified

**Created:**
- `src/classifier/__init__.py`
- `src/classifier/models.py`
- `src/classifier/rules.py`
- `src/classifier/classifier.py`
- `tests/test_classifier.py`
- `docs/ai_log/step-05.md`

**Modified:**
- None (existing models, sample generator, IR extractor, and prior tests remain untouched)

## Classification Logic & Scoring Design

1. **Safety Pre-checks**:
   - Documents with 0 pages or whitespace-only text immediately return `SKIP` with `score=0` and `confidence=0.0`.

2. **Negative / Irrelevant Signals**:
   - Matching indicators typical of invoices, shipping notes, resumes, or travel receipts (e.g. `"tax invoice"`, `"bill to"`, `"curriculum vitae"`) heavily penalize the score (-35 points per match).

3. **Multi-tiered Positive Signals**:
   - **Strong Domain Phrases (+35 pts each)**: High-precision domain terms such as `"portfolio management statement"`, `"client portfolio report"`, `"alternative investment fund"`, `"category ii aif"`, `"nav per unit"`.
   - **Statement Anchors (+15 pts each, capped at 45 pts)**: Structural anchors like `"statement period"`, `"client id"`, `"account number"`, `"portfolio summary"`.
   - **Supporting Vocabulary (+5 to +20 pts max)**: Generic terms like `"holdings"`, `"transactions"`, `"isin"`, `"dividend"`, `"market value"`. A single generic term scores only 5 points and is capped at 20 points, guaranteeing that generic terms alone cannot trigger `MAYBE` (25 pts) or `STATEMENT` (50 pts).
   - **Table Header Evidence (+15 to +35 pts max)**: Structural table inspection examining headers for holdings columns (`isin`, `security`, `qty`, `cost`, `market value`) or transaction columns (`trade date`, `activity`, `amount`).

4. **Thresholds & Decision Mapping**:
   - `score >= 50`: `STATEMENT` (confidence `0.80`–`0.99`). Statement type is identified as `StatementType.PMS` or `StatementType.AIF` based on dominant domain indicators.
   - `25 <= score < 50`: `MAYBE` (confidence `0.40`–`0.70`). Routed to manual review queue.
   - `score < 25`: `SKIP` (confidence `0.00`–`0.30`). Rejected as non-statement.

## Tests Run

Ran:
```bash
PYTHONPATH=. pytest -v
```

Output:
```
============================== 76 passed in 0.69s ==============================
```

Covering:
- Format A1 PMS statement classified as `STATEMENT` with `StatementType.PMS`
- Format A variants (A-messy, A-drift) classified as `STATEMENT` with `StatementType.PMS`
- Format B PMS statement classified as `STATEMENT` with `StatementType.PMS`
- Format C AIF statement classified as `STATEMENT` with `StatementType.AIF`
- Invoices and non-financial documents classified as `SKIP`
- Single generic financial words (e.g. meeting notes mentioning `"dividend"` or `"investment"`) classified as `SKIP`
- Ambiguous summary notices without statement tables classified as `MAYBE`
- 0-page and whitespace-only documents safely returning `SKIP` with score 0
- All Step 1 (models), Step 2 (sample generator), and Step 3 (IR extraction) tests passing without regressions

## Limitations

1. **Language Scope**:
   Rules and keyword dictionaries are tailored for English financial statements (standard for Indian PMS and AIF investment statements).
2. **Text-dependent**:
   The classifier evaluates textual and tabular tokens extracted from `DocumentIR`. Purely scanned images without an OCR text layer will yield zero text and thus classify as `SKIP`.
