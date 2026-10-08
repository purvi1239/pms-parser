# Step 02: Core Domain Models & Status Enums (Build Step 1)

## What Was Implemented
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

## Files Created / Modified
- `src/__init__.py`: Package marker for `src`.
- `src/models/__init__.py`: Re-exports domain models and enums.
- `src/models/enums.py`: Defines `StatementType`, `TransactionType`, and `DocumentStatus`.
- `src/models/models.py`: Defines `Holding`, `Transaction`, `Summary`, `Statement`, and `ParseResult`.
- `tests/test_models.py`: Focused test suite for models and enums.
- `docs/ai_log/step-02.md`: Documentation for this step.

## Tests Run
Executed test suite with pytest under Python 3.14 via `.venv`:
```bash
source .venv/bin/activate && PYTHONPATH=. pytest -v
```
Results:
- `tests/test_models.py::test_model_creation` PASSED
- `tests/test_models.py::test_decimal_values` PASSED
- `tests/test_models.py::test_default_empty_holdings` PASSED
- `tests/test_models.py::test_enum_status_values` PASSED
- `tests/test_models.py::test_basic_pydantic_validation` PASSED
- `tests/test_models.py::test_json_roundtrip_and_precision` PASSED
- `tests/test_models.py::test_aif_structure` PASSED

**Total**: 7 passed in 0.10s.

## Design Decisions
1. **Decimal for Monetary and Quantitative Values**: All monetary amounts, quantities, NAVs, and weights use `Decimal` to avoid IEEE 754 floating-point inaccuracies.
2. **Case-Insensitive Enums**: Enums implement `_missing_` classmethods so string inputs like `"pms"`, `"PMS"`, `"buy"`, or `"BUY"` resolve to the canonical enum members.
3. **Empty List Default for Holdings**: `holdings` defaults to a factory-generated empty list `[]` rather than `None`. This supports AIFs (which naturally have 0 holdings) and allows PMS zero-holdings validation rules to check `len(holdings) == 0`.
4. **ParseResult as Simple Container**: `ParseResult` is kept as a simple, transparent container (`statement`, `holdings`, `transactions`, `summary`, `status`, `warnings`, `errors`, `raw_metadata`) without implicit model validator synchronization logic.
5. **Separation of Model Validation from Business Validation**: Models permit constructing partial and raw data representations (e.g. `amount` is stored signed without crashing if raw data is momentarily unsorted), deferring comprehensive cross-field identity rules (cash identity, value identity, sign-vs-type match) to the dedicated Validator module in Step 9.

## Assumptions Made
1. Non-identity fields across `Holding`, `Transaction`, and `Summary` are optional (`None` default) to allow partial parses and flexible extraction from heterogeneous statement formats.
2. `Statement.currency` defaults to `None` (no assumption of INR). The parser extracts the currency from the source statement when available.
3. Transaction dates accept either standard `datetime.date` objects or date strings to ease raw data ingestion prior to canonical parsing.
