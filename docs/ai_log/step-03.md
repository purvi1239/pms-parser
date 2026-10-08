# Step 03: Sample Generator (Build Step 2)

## What Was Implemented

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

## Files Created / Modified

**Created:**
- `src/sample_generator/_isin.py`
- `src/sample_generator/_data.py`
- `src/sample_generator/_pdf.py`
- `src/sample_generator/_ground_truth.py`
- `src/sample_generator/formats/__init__.py`
- `src/sample_generator/formats/format_a.py`
- `src/sample_generator/formats/format_a_messy.py`
- `src/sample_generator/formats/format_a_drift.py`
- `src/sample_generator/formats/format_b.py`
- `src/sample_generator/formats/format_c.py`
- `src/sample_generator/generator.py`
- `src/sample_generator/__init__.py`
- `src/sample_generator/__main__.py`
- `tests/test_sample_generator.py`
- `docs/ai_log/step-03.md`

**Modified:**
- `.gitignore` — added `samples/synthetic/` (generated files must not be committed)

**Generated (runtime, not committed):**
- `samples/synthetic/format_a1.pdf` + `.json`
- `samples/synthetic/format_a2.pdf` + `.json`
- `samples/synthetic/format_b.pdf` + `.json`
- `samples/synthetic/format_c.pdf` + `.json`
- `samples/synthetic/format_a_messy.pdf` + `.json`
- `samples/synthetic/format_a_drift.pdf` + `.json`
- `samples/synthetic/format_a1_encrypted.pdf` + `.json`

## Sample Variants Generated

| Label | Type | Pages | Special Features |
|-------|------|-------|-----------------|
| `format_a1` | PMS | 1 | 5 holdings, 7 transactions, full summary |
| `format_a2` | PMS | 1 | Different quarter, 4 holdings, 5 transactions |
| `format_b` | PMS | 1 | Green theme, horizontal summary, `YYYY-MM-DD` dates, different all headings |
| `format_c` | AIF | 1 | Empty holdings, NAV per unit, units, Cash Ledger section |
| `format_a_messy` | PMS | 2 | 10 holdings spanning 2 pages, repeated table header, 1 wrapped security name |
| `format_a_drift` | PMS | 1 | Renamed title & summary heading, reordered+renamed columns, `DD-Mon-YYYY` dates |
| `format_a1_encrypted` | PMS | 1 | RC4-encrypted clone of `format_a1`; password in `generator.ENCRYPTED_SAMPLE_PASSWORD` |

## Financial Consistency Properties

Every dataset is verified at construction (Python `assert`) before writing:

- **Cash identity**: `closing_cash = opening_cash + Σ(signed amounts)` — enforced in `SummaryData.verify_cash_identity()`
- **AIF NAV identity**: `|units × nav_per_unit - closing_value| ≤ 1% × closing_value` — enforced in `SummaryData.verify_nav_identity()`
- **Holdings weights ≈ 100%**: final holding's weight is adjusted to make the sum exact
- **ISIN check digits**: `make_isin()` uses the Luhn-variant algorithm; `validate_isin()` verifies any ISIN
- **Sign convention**: inflow/sell/dividend → `+`; buy/fee/outflow → `−`

## Tests Run

```
PYTHONPATH=. pytest -v
```

```
54 passed, 1 warning in 0.38s
```

47 new tests in `tests/test_sample_generator.py`:

| Test | What it verifies |
|------|-----------------|
| `test_pdf_exists_and_is_valid` × 7 | PDF file exists, non-zero, `%PDF-` header |
| `test_ground_truth_json_exists_and_loadable` × 7 | JSON exists, loads, has required keys |
| `test_cash_identity` × 6 | `closing_cash = opening_cash + Σ(txns)` |
| `test_holdings_weights_sum_to_100` × 5 | `|Σ(weight) - 100| ≤ 0.1` |
| `test_aif_nav_identity` | `units × nav ≈ closing_value` |
| `test_aif_has_no_holdings` | Format C has empty holdings list |
| `test_pms_has_holdings` × 4 | PMS formats have ≥ 1 holding |
| `test_holding_isins_are_valid` × 4 | All ISINs pass Luhn check |
| `test_transaction_sign_convention` × 5 | Buy/fee/outflow −; inflow/sell/dividend + |
| `test_encrypted_pdf_requires_password` | `is_encrypted=True`, empty password fails |
| `test_encrypted_pdf_decrypts_with_correct_password` | Correct password decrypts; pages accessible |
| `test_make_isin_produces_valid_isins` | All `SYNTHETIC_ISINS` pass `validate_isin` |
| `test_validate_isin_rejects_bad_isins` | Mutated ISIN rejected |
| `test_generate_all_idempotent` | Second run succeeds, all files present |
| `test_format_a_messy_has_more_holdings_than_a1` | A-messy > A1 holding count |
| `test_format_a_messy_has_wrapped_name` | At least one name > 40 chars |

## Design Decisions

1. **`_data.py` is the single source of truth**: All financial values are defined as `Decimal` constants. The `verify()` method on `SampleData` runs all consistency checks at Python import time so a broken dataset can never reach the PDF builder or JSON writer.

2. **ISIN Luhn implementation**: Implemented from scratch rather than a library to avoid a dependency and to have a `validate_isin` function usable in later tests (validator module, Step 10). The algorithm converts each character to a numeric string, then applies the standard Luhn doubling on the combined digit sequence.

3. **PDF layout anchors are stable**: Section headings (`"Holdings"`, `"Transactions"`, `"Portfolio Summary"`) are literal strings repeated consistently across Format A, A-messy, and A-drift (only A-drift mutates them). This ensures the fingerprint module can distinguish formats without false negatives.

4. **A-drift mutations are deliberately minimal and structural**: Only structural anchor changes are made (title, column header names, column order, date format). Data values are identical to A1. This is the most realistic way a fund house would accidentally break a spec — a template redesign, not new data.

5. **Encrypted sample password stored in `generator.py` only**: The constant `ENCRYPTED_SAMPLE_PASSWORD = "TestPass@123"` lives in the test-support module. It is never in `.env`, never in `.env.example`, and the `samples/synthetic/` directory is `.gitignore`-d so the encrypted PDF is also not committed.

6. **`samples/synthetic/` added to `.gitignore`**: Generated binary artefacts (PDFs) should not be version-controlled. The generator is the source of truth; CI will regenerate them.

## Limitations

1. **PyPDF2 deprecation warning**: PyPDF2 3.0.1 prints a deprecation warning in favour of `pypdf`. Since the password-handling pipeline test only needs to verify encrypt/decrypt round-trips at this stage, PyPDF2 is sufficient. The plan notes PyMuPDF as a future fallback in pdf_extract (Step 4); `pypdf` can replace PyPDF2 for the encryption test at that point with no API changes.

2. **RC4 encryption only**: PyPDF2 `encrypt()` uses RC4, not AES-256. Real PMS PDFs may use AES. The encrypted sample serves only to exercise the "detect password-protected → try password → succeed" pipeline path; the encryption strength is not the focus here.

3. **Synthetic ISINs are not real exchange-registered securities**: ISINs are constructed with valid check digits but arbitrary bodies. This is intentional for synthetic data isolation.
