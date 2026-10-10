# Step 11: PII Redactor (Build Step 10)

## What Was Implemented

Implemented a dedicated, deterministic PII redactor in `src/redactor/` following Project Plan v4.
The redactor identifies and masks sensitive personal identifiers in text before text is transmitted to an LLM for parsing specification generation.

The module operates entirely in memory using regex patterns and contextual keyword anchors. It is completely independent of the LLM adapter, parsing engine, and storage layer. It never transmits data to an API, and it strictly ensures that original sensitive values are never retained in metadata, logs, or error messages.

---

## Modules Created

### `src/redactor/models.py`

Pydantic data models for configuration, metadata items, and redaction results:

| Model | Purpose |
|---|---|
| `EntityType` | Enum of supported sensitive types (`EMAIL`, `PHONE`, `PAN`, `AADHAAR`, `BANK_ACCOUNT`, `DEMAT`, `NAME`, `CUSTOM`). |
| `RedactionConfig` | Feature toggles per entity type, custom sensitive values list, token indexing toggle, and custom token template mappings. |
| `RedactionItem` | Structured metadata for each redacted match: `entity_type`, `token`, character offsets (`start_char`, `end_char`), `value_length`, and truncated SHA-256 `value_hash`. Original plaintext value is strictly excluded. |
| `RedactionResult` | Container holding `redacted_text`, list of `RedactionItem`s, `counts_by_type`, and `total_redactions`. |

### `src/redactor/patterns.py`

Compiled regular expressions and detection guardrails tailored for financial statement contexts:
- Email addresses (`EMAIL_PATTERN`).
- Indian PAN 10-char alphanumeric sequence (`PAN_PATTERN`).
- Indian mobile and landline phone numbers with negative lookahead/lookbehind against currency symbols and decimals (`PHONE_PATTERN`).
- Formatted (4-4-4) and labeled Aadhaar numbers (`AADHAAR_FORMATTED_PATTERN`, `AADHAAR_LABELED_PATTERN`).
- Labeled bank account numbers (`BANK_ACCOUNT_PATTERN`).
- Labeled and standalone NSDL/CDSL Demat accounts (`DEMAT_LABELED_PATTERN`, `DEMAT_STANDALONE_NSDL_PATTERN`).
- Contextually labeled client/investor names (`NAME_LABELED_PATTERN`).
- Guard functions: `is_isin()` and `is_financial_number()` to prevent false positives.

### `src/redactor/redactor.py`

- `Redactor`: Core class orchestrating span collection, overlap resolution, consistent token assignment, text replacement, and metadata generation.
- `redact()`: Module-level convenience function for one-off string sanitization.

### `src/redactor/__init__.py`

Public API exports for `Redactor`, `redact`, `EntityType`, `RedactionConfig`, `RedactionItem`, and `RedactionResult`.

---

## Supported Entity Types & Replacement Tokens

| Entity Type | Detection Mechanism | Default Token | Guardrail / Boundary Check |
|---|---|---|---|
| **Email** | Standard RFC-compliant regex | `[REDACTED_EMAIL]` | Word boundaries |
| **PAN** | 5 uppercase letters + 4 digits + 1 uppercase letter | `[REDACTED_PAN]` | Checked against surrounding text to never corrupt 12-char ISINs |
| **Phone** | Indian 10-digit mobile (`[6-9]\d{9}`) and landlines with `+91`/`0` prefixes | `[REDACTED_PHONE]` | Excluded if preceded by currency (`₹`, `INR`, `$`) or followed by decimals (`.00`) |
| **Aadhaar** | 12-digit number (starts 2-9) in 4-4-4 format or labeled with UID/Aadhaar | `[REDACTED_AADHAAR]` | Not applied to bare 12-digit numbers without formatting or labels to avoid colliding with quantities |
| **Bank Account** | 9–18 digit sequences preceded by `A/c`, `Account No`, etc. | `[REDACTED_BANK_ACCOUNT]` | Requires anchor prefix to avoid matching monetary amounts |
| **Demat Account** | 16-digit CDSL or `IN`+14 digit NSDL preceded by `Demat`, `DP ID`, `BO ID` | `[REDACTED_DEMAT_ACCOUNT]` | Standalone `IN\d{14}` is 16 chars, distinct from 12-char ISINs |
| **Name** | Names preceded by `Investor Name:`, `Client Name:`, etc. | `[REDACTED_NAME]` | Boundary matches before delimiters or newlines |
| **Custom Values** | Caller-supplied explicit values | `[REDACTED_CUSTOM]` | Case-insensitive substring matching |

---

## Consistent Masking & Security Guarantees

1. **Consistent Token Assignment**:
   Repeated occurrences of the exact same sensitive value within a single redaction operation always receive the identical replacement token.
   - Standard mode: every occurrence of `user@example.com` becomes `[REDACTED_EMAIL]`.
   - Indexed mode (`use_indexed_tokens=True`): occurrences of `user1@example.com` receive `[REDACTED_EMAIL_1]`, while `user2@example.com` receives `[REDACTED_EMAIL_2]`.
2. **Zero Sensitive Value Leakage**:
   The `RedactionItem` and `RedactionResult` models store only character offsets, replacement token names, value length, and a 12-character truncated SHA-256 hash. The plaintext sensitive value is discarded immediately after span replacement.
3. **Preservation of Document Structure**:
   Table headers, dates (`01 January 2026 to 31 March 2026`), ISIN codes (`INE002A01018`), monetary values (`₹ 10,00,000.00`, `-45000.00`), portfolio weights (`15.5%`), and quantities (`150`) are fully preserved so that the learner LLM can still recognize table structures and generate accurate parsing specifications.

---

## Tests Created

**`tests/test_redactor.py`** — 13 test classes covering 25 test cases:

| Class | What it covers |
|---|---|
| `TestEmptyAndCleanText` | Empty string, whitespace-only string, clean financial header text |
| `TestEmailRedaction` | Single email, multiple distinct emails, subdomains, plus-tags |
| `TestPANRedaction` | Valid PAN detection, strict guard ensuring 12-char ISINs are never redacted |
| `TestPhoneRedaction` | Indian mobile numbers (+91, spaces, hyphens, 0-prefix), landlines, financial amount guard |
| `TestAadhaarRedaction` | 4-4-4 formatted Aadhaar, labeled unformatted Aadhaar, date preservation |
| `TestBankAccountRedaction` | Labeled bank accounts, masked account numbers (`XXXXXXXX1234`) |
| `TestDematAccountRedaction` | CDSL 16-digit accounts, NSDL labeled DP ID / Client ID, standalone NSDL |
| `TestNameRedaction` | Contextually labeled investor names and client names |
| `TestCustomSensitiveValues` | Explicit caller-supplied values passed to `redact()` or configured in `RedactionConfig` |
| `TestConsistentMasking` | Repeated values masked with identical standard tokens and indexed tokens (`_1`, `_2`) |
| `TestNoSensitiveValueLeakage` | Strict verification that raw plaintext PII does not appear in serialized metadata |
| `TestConfigurableToggles` | Disabling individual entity types (`redact_email=False`), custom token templates |
| `TestRealisticStatementPreservation` | Full PMS statement snippet with client info, holdings, transactions, and summary: PII masked while all financial figures, ISINs, dates, and anchors remain intact |

---

## Files Created / Modified

| File | Status | Role |
|---|---|---|
| `src/redactor/__init__.py` | Created | Package exports |
| `src/redactor/models.py` | Created | `EntityType`, `RedactionConfig`, `RedactionItem`, `RedactionResult` |
| `src/redactor/patterns.py` | Created | Regex patterns, entity matchers, and false-positive guard functions |
| `src/redactor/redactor.py` | Created | `Redactor` class and `redact()` convenience function |
| `tests/test_redactor.py` | Created | Automated unit tests covering all detection and preservation cases |
| `docs/ai_log/step-11.md` | Created | Implementation log and design documentation |

---

## Known Limitations

1. **Contextual Dependency for Bank Accounts and Names**:
   Because raw numbers (e.g. 10–16 digits) and person names (capitalized words) would otherwise clash with transaction numbers or security names (e.g. "Reliance Industries Limited"), bank accounts and names are only recognized when preceded by anchor labels (such as `Account No:`, `Investor Name:`) or supplied explicitly via `custom_sensitive_values`.
2. **Multi-line Names**:
   Names that wrap across multiple physical lines without standard separators may only have their first line matched. Callers can supply known investor names via `custom_sensitive_values` to ensure complete coverage.
3. **Non-Indian Formats**:
   The built-in patterns are optimized for Indian financial statements (PAN, Aadhaar, Indian 10-digit mobile numbers, NSDL/CDSL Demat). Foreign phone numbers, SSNs, or international IBANs should be masked by supplying custom regexes or explicit tokens.

---

## Terminal Commands to Verify Later

```bash
# Run the redactor unit tests
PYTHONPATH=. ./.venv/bin/pytest tests/test_redactor.py -v

# Run with concise tracebacks
PYTHONPATH=. ./.venv/bin/pytest tests/test_redactor.py -v --tb=short

# Run full project test suite excluding legacy fingerprint test
PYTHONPATH=. ./.venv/bin/pytest tests/ -v --ignore=tests/test_fingerprint.py
```
