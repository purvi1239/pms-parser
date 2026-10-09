# Step 06: Format Fingerprinting and Template Drift Detection

## What Was Implemented

Implemented the `src/fingerprint/` module for extracting structural layout feature sets from `DocumentIR` and comparing them using configurable similarity scoring. Rather than relying on rigid raw byte hashes, this module uses a feature-based representation capable of recognizing known templates, detecting subtle template mutations (drift), and categorizing unknown layouts.

### Components Created

| File / Component | Purpose |
|------------------|---------|
| `src/fingerprint/models.py` | `FormatFingerprint` feature container (`anchor_phrases`, `table_headers`, `layout_stats`), `LayoutStats`, `FingerprintMatchStatus` enum (`KNOWN`, `DRIFT_SUSPECTED`, `UNKNOWN`), and `FingerprintMatchResult` |
| `src/fingerprint/extractor.py` | `extract_fingerprint(doc, format_id=None) -> FormatFingerprint`: extracts normalized text anchors, ordered table headers, and layout statistics while filtering out dynamic numbers/amounts |
| `src/fingerprint/comparator.py` | `compare_fingerprints(fp1, fp2, match_threshold=0.90, near_match_threshold=0.65)` and `match_fingerprint(query_fp, library)` computing weighted multi-component similarity |
| `src/fingerprint/__init__.py` | Public interface re-exporting extractor, comparators, and domain models |
| `tests/test_fingerprint.py` | 9 focused test cases verifying identical matches, similar statements across periods, multi-page repeated headers, column-reordered drift detection, unrelated formats, empty documents, library catalog matching, and threshold overrides |

## Files Created / Modified

**Created:**
- `src/fingerprint/__init__.py`
- `src/fingerprint/models.py`
- `src/fingerprint/extractor.py`
- `src/fingerprint/comparator.py`
- `tests/test_fingerprint.py`
- `docs/ai_log/step-06.md`

**Modified:**
- None (all previous modules, generator, and test suites left untouched)

## Feature Extraction & Similarity Design

1. **Structured Feature Set (`FormatFingerprint`)**:
   - `anchor_phrases: list[str]`: Normalized, lowercased structural text headings and metadata field labels (e.g. `"portfolio management statement"`, `"holdings"`, `"statement period"`). Purely dynamic tokens (numbers, currency values, dates) are filtered out.
   - `table_headers: list[list[str]]`: Normalized ordered lists of column header tokens per table (e.g. `['security name', 'isin', 'qty', 'cost inr', 'mkt value inr', 'weight']`). Deduplicates consecutive repeated headers from multi-page table splits.
   - `layout_stats: LayoutStats`: Geometric and structural layout metrics (`page_count`, `table_count`, `column_counts`).

2. **Multi-Component Similarity Scoring**:
   - **Table Header Similarity (50% weight)**: Compares table headers pairwise, combining Jaccard vocabulary overlap with strict positional sequence matching (`matches / max_len`). When column order changes (as in Format A-drift where ISIN moved to the end), positional score drops while Jaccard remains high.
   - **Anchor Phrase Similarity (35% weight)**: Computes Jaccard set overlap across structural anchors.
   - **Layout Statistics Similarity (15% weight)**: Compares column count sequences and table count proximity.
   - **Composite Score**: `0.50 * table_sim + 0.35 * anchor_sim + 0.15 * layout_sim`.

3. **Classification Thresholds**:
   - `similarity >= match_threshold` (default `0.90`): `KNOWN` — document shares identical structural template and can be safely parsed by the existing spec.
   - `near_match_threshold <= similarity < match_threshold` (default `0.65` to `0.90`): `DRIFT_SUSPECTED` — document is recognizable as a mutation of an existing spec, triggering quarantine rather than silent LLM re-prompting.
   - `similarity < near_match_threshold` (default `< 0.65`): `UNKNOWN` — genuinely new format requiring first-time learning.

## Bug Fix: Data-Value Contamination in Anchor and Table Feature Extraction

Initial implementation passed 6/9 tests. Root cause was two sources of data-value contamination that caused A1 vs A2 and A1 vs A-messy to score `DRIFT_SUSPECTED` instead of `KNOWN`.

### Problem 1 — Anchor pollution (anchor_sim = 0.48 for A1 vs A2)

Summary table rows were rendered as plain text lines and captured as structural anchors. Lines such as `"Opening Cash Balance ₹80,000.00"` normalized to `"opening cash balance n80 000 00"`, which contained the data value (`n80`, `000`, `00`). Because A1 and A2 have different cash balances, the anchor sets diverged, cutting anchor Jaccard from 1.0 to ~0.48.

**Fix:** Added `_contains_data_value(norm)` which rejects any phrase containing bare digit tokens (`\d+`) or letter-prefixed amount tokens (`[a-z]\d+`). `_is_static_anchor_candidate` now calls this filter. Currency row text is silently dropped; only structural labels (section headings, metadata keys) remain as anchors.

### Problem 2 — Summary table header contamination (table_sim = 0.81 for A1 vs Messy)

The 2-column summary table `["Opening Portfolio Value", "₹1,200,000.00"]` was fingerprinted as a table with `get_headers()` returning `["opening portfolio value", "n1 200 000 00"]`. The monetary cell (`n1 200 000 00`) differed between documents, penalising the pairwise table score.

**Fix:** Added `_is_structural_header_row(norm_cells)` which requires `len(norm_cells) >= 3` (excluding 2-column label-value summary tables) and no data-value cells. Tables failing this check are excluded from `table_headers` and `layout_stats`.

### Confirmed scores after fix

| Pair | table | anchor | layout | composite | status |
|------|-------|--------|--------|-----------|--------|
| A1 vs A2 | 1.0000 | 1.0000 | 1.0000 | **1.0000** | KNOWN |
| A1 vs Messy | 1.0000 | 1.0000 | 1.0000 | **1.0000** | KNOWN |
| A1 vs Drift | 0.7202 | 0.6667 | 1.0000 | **0.7435** | DRIFT_SUSPECTED |
| A1 vs B | 0.0000 | 0.0526 | 0.4333 | **0.0834** | UNKNOWN |
| A1 vs C | 0.0000 | 0.0000 | 0.1500 | **0.0225** | UNKNOWN |

Anchors for A1 and A2 are now identical (10 structural labels, no data values):
```
['portfolio management statement', 'pinnacle wealth pms india growth strategy',
 'client id', 'currency', 'inr statement type', 'pms', 'holdings',
 'transactions', 'date type security qty amount inr', 'portfolio summary']
```

Table headers fingerprinted (summary table excluded):
```
[['security name', 'isin', 'qty', 'cost inr', 'mkt value inr', 'weight %'],
 ['date', 'type', 'security', 'qty', 'amount inr']]
```

## Empirical Verification on Synthetic Documents

- `format_a1` vs `format_a1`: Score = `1.000` → `KNOWN`
- `format_a1` vs `format_a2`: Score = `1.000` → `KNOWN` (identical template, different quarterly data)
- `format_a1` vs `format_a_messy`: Score = `1.000` → `KNOWN` (multi-page, repeated headers)
- `format_a1` vs `format_a_drift`: Score = `0.744` → `DRIFT_SUSPECTED` (renamed title + reordered columns)
- `format_a1` vs `format_b`: Score = `0.083` → `UNKNOWN`
- `format_a1` vs `format_c`: Score = `0.023` → `UNKNOWN`
- Sparse / empty documents: Score = `0.000` → `UNKNOWN`

## Tests Run

```bash
PYTHONPATH=. pytest tests/test_fingerprint.py -v
# 9 passed in 199.73s (slow due to ReportLab + pdfplumber cold import)
```

## Limitations

1. **Threshold sensitivity**:
   Thresholds default to `0.90` for `match_threshold` and `0.65` for `near_match_threshold`. These defaults separate synthetic variants cleanly, but are exposed as configuration parameters in `compare_fingerprints` and `match_fingerprint` to support per-deployment tuning if real-world templates exhibit unexpected variance.
2. **Table order alignment**:
   Pairwise table comparison currently compares tables in their natural extraction order. If a provider renders independent tables in swapped vertical order, alignment based on best-match bipartite pairing could be added in the future.
