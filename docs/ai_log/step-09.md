# Step 09: Statement Validator (Build Step 8)

## What Was Implemented

Implemented a deterministic, non-mutating statement validator in `src/validator/`.
The validator accepts a `ParseResult` (from the parsing engine) and runs a set of
financial consistency checks, returning a structured `ValidationResult`.

No LLM calls, storage I/O, or pipeline orchestration were added.
No extracted values are modified by any check.

---

## Modules Created

### `src/validator/models.py`

Three Pydantic models:

| Class | Purpose |
|---|---|
| `ValidationSeverity` | `ERROR` (data issue → NEEDS_REVIEW) · `WARNING` (incomplete/advisory) |
| `ValidationFinding` | Single structured finding: `check`, `severity`, `message`, `field`, `actual`, `expected` |
| `ValidationResult` | Aggregate: `findings`, `checks_run`, `checks_skipped`, `final_status` with `.errors`, `.warnings`, `.passed` properties |

### `src/validator/config.py`

`ValidatorConfig` (Pydantic) holds configurable tolerances matching Project Plan v4:

| Field | Default | Description |
|---|---|---|
| `cash_tolerance` | `1.00` | Max absolute cash deviation (currency units) |
| `weights_tolerance_pct` | `0.50` | Max Σ(weight) deviation from 100% (percentage points) |
| `nav_tolerance_pct` | `1.00` | Max relative NAV deviation (percentage) |
| `check_quantity_price` | `True` | Toggle quantity × price consistency check |
| `price_tolerance` | `1.00` | Max absolute deviation between quantity × price and value |
| `price_tolerance_pct` | `1.00` | Max relative deviation percentage between quantity × price and value |
| `max_transaction_charge_pct` | `2.00` | Max transaction charges as percentage of gross trade value |
| `check_transaction_signs` | `True` | Toggle sign-vs-type check |
| `check_isin_checksums` | `True` | Toggle ISIN Luhn check |
| `check_transaction_dates` | `True` | Toggle date-in-period check |

### `src/validator/checks.py`

Eight individual check functions, each returning `(findings, was_run: bool)`:

#### Check 1 — `check_identity_fields`
Structural completeness warnings:
- PMS with zero holdings → WARNING
- AIF with no `units` and no `nav_per_unit` → WARNING
- Missing `client_identifier` → WARNING
- Missing `provider_name` → WARNING
- Summary present but all primary monetary fields None → WARNING

#### Check 2 — `check_cash_reconciliation`
`closing_cash = opening_cash + Σ(transaction.amount)` (Plan v4 Cash identity).
- Skipped when `opening_cash`, `closing_cash`, or transactions are absent.
- Null transaction amounts → WARNING (degraded, check still runs).
- Deviation > `cash_tolerance` → **ERROR**.

#### Check 3 — `check_nav_consistency` *(AIF only)*
`units × nav_per_unit ≈ closing_value` (Plan v4 NAV identity).
- Skipped for PMS statements.
- Missing any of the three values → WARNING (not silently skipped).
- Relative deviation > `nav_tolerance_pct` % → **ERROR**.

#### Check 4 — `check_portfolio_weights`
`Σ(holding.weight) ≈ 100%`.
- Skipped when no holding has a weight.
- Some but not all holdings have weight → WARNING (partial data).
- All weights present and deviation > `weights_tolerance_pct` → **ERROR**.

#### Check 5 — `check_quantity_price_value`
`quantity × price ≈ transaction amount / holding market_value`.
- Skipped when disabled or neither transactions nor holdings have both quantity and price.
- If quantity and price are present but amount is None → **WARNING** (incomplete data).
- **BUY**: Gross value = `quantity × price`. Cash outflow (`|amount|`) must be `≥ gross - tolerance`. Implied charges (`|amount| - gross`) must not exceed `max_transaction_charge_pct` → **ERROR** if violated.
- **SELL**: Gross value = `quantity × price`. Cash inflow (`amount`) must be `≤ gross + tolerance`. Implied charges (`gross - amount`) must not exceed `max_transaction_charge_pct` → **ERROR** if violated.
- **Holdings**: If holding has `quantity`, `price`, and `market_value`, verifies `quantity × price ≈ market_value` within tolerance → **ERROR** if violated.

#### Check 6 — `check_transaction_signs`
Each transaction's `amount` sign must match its `type` (Plan v4 sign convention):

| Type | Expected sign |
|---|---|
| INFLOW, SELL, DIVIDEND | positive (> 0) |
| BUY, FEE, OUTFLOW | negative (< 0) |

Zero or None amounts are skipped. Violation → **ERROR** per transaction.

#### Check 7 — `check_isin_checksums`
ISIN Luhn check digit validation (implemented inline; does not import from `sample_generator`).
- None/empty ISINs skipped (missing ISIN is handled by identity check).
- Invalid format (length ≠ 12, last char not digit) → **ERROR**.
- Valid format but wrong check digit → **ERROR**.

#### Check 8 — `check_transaction_dates`
Each `transaction.date` (as `datetime.date`) must fall within the parsed statement period.
- `statement_period` parsing supports 4 common formats: "DD Month YYYY to DD Month YYYY",
  "Month DD, YYYY to Month DD, YYYY", ISO, and DD/MM/YYYY.
- Out-of-period dates → **WARNING** (not ERROR, because period string parsing is best-effort).
- String `date` values (unrecognised format, stored as raw string by engine) are skipped.

### `src/validator/validator.py`

`Validator` class runs all checks in `ALL_CHECKS` order, accumulates findings, then computes:

```
ERROR finding present       → final_status = NEEDS_REVIEW
WARNING only, no errors     → final_status = PARSED_WITH_WARNINGS
No findings                 → final_status = PARSED
```

`validate_result(result, config=None)` is a convenience function.

Defensive exception handling around each check ensures a bug in one check cannot abort validation of remaining checks.

### `src/validator/__init__.py`

Package exports for all public symbols.

---

## Tests Created

**`tests/test_validator.py`** — 12 test classes, ~74 test cases:

| Class | What it covers |
|---|---|
| `TestValidatorContract` | API contract: return types, `.passed`, `.errors`/`.warnings` properties |
| `TestCashReconciliation` | Valid, error, within tolerance, over tolerance, null amounts, skipped cases |
| `TestNAVConsistency` | Valid AIF, error, within tolerance, PMS skipped, missing fields warning |
| `TestPortfolioWeights` | Sum to 100, error, tolerance boundary (just within, just over), partial weights |
| `TestQuantityPriceValue` | BUY exact match, acceptable charges, excessive charges error, underpayment error, SELL exact match, acceptable charges, overpayment error, excessive charges error, missing amount warning, holding match/mismatch, skipped without price, disabled |
| `TestTransactionSigns` | 12 parametrized sign cases (6 types × correct/wrong), zero amount, None amount, disabled |
| `TestISINChecksum` | 11 parametrized ISINs (valid/invalid), mixed, None, disabled, format error |
| `TestTransactionDates` | In-period, before-start, after-end, boundary, 4 period formats, unparseable, disabled |
| `TestIdentityFields` | All warnings, PMS zero holdings, missing client/provider, AIF NAV, AIF no-holdings OK |
| `TestFullValidatorIntegration` | Clean PMS pass, cash error → NEEDS_REVIEW, clean AIF pass, multiple errors accumulated |
| `TestValidatorConfig` | Tighter tolerance triggers error, disable ISIN, disable sign checks |
| `TestValidateResultConvenience` | Default config, custom config passed through |

---

## Files Created / Modified

| File | Status | Role |
|---|---|---|
| `src/validator/__init__.py` | Created | Package exports |
| `src/validator/config.py` | Created | `ValidatorConfig` tolerance model |
| `src/validator/models.py` | Created | `ValidationFinding`, `ValidationResult`, `ValidationSeverity` |
| `src/validator/checks.py` | Created | All 8 check functions + ISIN Luhn helper + period parser |
| `src/validator/validator.py` | Created | `Validator` class and `validate_result()` |
| `src/models/models.py` | Modified | Added optional `price` field (`Decimal | None = None`) to `Holding` and `Transaction` |
| `tests/test_validator.py` | Created | ~74 focused unit tests |
| `docs/ai_log/step-09.md` | Created | This implementation log |

---

## Terminal Commands to Run

```bash
# Fast unit tests only (no PDF I/O — completes in seconds once imports are loaded)
PYTHONPATH=. ./.venv/bin/pytest tests/test_validator.py -v

# With verbose output including parametrize labels
PYTHONPATH=. ./.venv/bin/pytest tests/test_validator.py -v --tb=short

# Run just one class
PYTHONPATH=. ./.venv/bin/pytest tests/test_validator.py::TestCashReconciliation -v

# Run all tests including previous steps (regression)
PYTHONPATH=. ./.venv/bin/pytest tests/ -v --ignore=tests/test_fingerprint.py
```

> **Note:** Python 3.14 + pdfplumber startup is ~60–90 s on this system due to import overhead. The validator tests themselves contain no PDF operations and execute instantaneously after collection.

---

## Design Decisions

1. **Warnings-not-errors for ambiguous cases**: Out-of-period dates and partial-weight data are WARNING severity because parsing is best-effort; the validator avoids false positives when information is incomplete.

2. **Inline ISIN Luhn implementation**: Production code never imports from `src/sample_generator/` (a dev-only module). The check digit algorithm is self-contained in `checks.py` (7 lines).

3. **Period parsing is best-effort**: 4 common formats are handled. Unknown formats silently skip the date check rather than warning — an unparseable period string is a parser limitation, not a validation finding.

4. **Defensive check execution**: Each check is wrapped in `try/except` in the Validator loop. A bug in one check cannot prevent others from running.

5. **`was_run` return flag**: Distinguishes "check ran and passed" from "check was skipped due to missing data". Callers can distinguish these in `checks_run` vs `checks_skipped` lists.

---

## Known Limitations

1. **Cash check assumes all amounts are in the same currency**: No currency conversion is performed. Multi-currency statements would fail reconciliation.

2. **Date check is warning-only**: Out-of-period transaction dates are common in real statements (e.g., settlement date vs trade date, prior-period adjustments). Raising an ERROR would cause too many false positives.

3. **No Value identity check**: The plan's Value identity (`closing = opening + inflows − outflows + income − fees ± gains`) is not implemented. Computing this requires knowing which summary fields (realized/unrealized gains) are present, and the engine often extracts only a subset of summary fields from a PDF. This would generate many false positives with incomplete summaries.

4. **Weights check requires all weights**: If the engine only extracts weights for some holdings (e.g., the last holding row has no weight due to a PDF layout edge case), the check degrades to a WARNING rather than detecting a real error.

5. **ISIN check requires 12-char format**: Some proprietary systems use non-standard security identifiers. These will fail the ISIN check. Consider suppressing the check per-holding when the security identifier is known to be non-ISIN.

6. **No cross-statement validation**: The validator checks a single ParseResult in isolation. Comparing two consecutive statements (e.g., last period's closing_cash matches this period's opening_cash) is outside scope.
