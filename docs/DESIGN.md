# System Architecture & Design Overview

The **PMS & AIF Investment Statement Parser** is a deterministic, template-driven ingestion, parsing, and financial validation engine designed for Indian investment portfolios.

---

## 1. High-Level Architecture

The system pipeline follows a multi-stage deterministic processing flow:

```
[PDF / Email Ingestion]
        │
        ▼
 [DocumentIR Extractor]  (Positional tokens, tables, metadata)
        │
        ▼
 [Layout Fingerprinter]  (Extracts geometric & lexical layout tokens)
        │
        ├─────────────────────────────┬──────────────────────────────┐
        ▼                             ▼                              ▼
Exact Match (Score >= 0.90)   Drift (0.65 <= Score < 0.90)    Unknown (Score < 0.65)
        │                             │                              │
        ▼                             ▼                              ▼
 [Template Engine]             [QUARANTINE]                  [NEEDS_REVIEW]
  - Section Parsing             (DRIFT_SUSPECTED)             (Optional: Learner + PII Redactor)
  - Table Parsing                                                    │
        │                                                            ▼
        ▼                                                    [Draft Spec Proposal]
[Financial Validator]                                         (Requires Manual Approval)
  - Cash Reconciliation
  - Portfolio Weights
  - ISIN Checksums
  - NAV Consistency
        │
        ▼
[Statement Store (SQLite)]
```

---

## 2. Core Modules

| Module | Location | Responsibility |
|---|---|---|
| **Ingestion** | `src/ingestion/` | Folder monitoring and read-only Gmail IMAP attachment fetching. |
| **Intermediate Rep (IR)** | `src/ir/` | Extraction of spatial text, bounding boxes, and tables into `DocumentIR`. |
| **Fingerprint** | `src/fingerprint/` | Spatial and textual hashing to match documents to templates or detect drift. |
| **Specs & Store** | `src/specs/` | JSON-based deterministic parsing rules and specification repository. |
| **Engine** | `src/engine/` | Deterministic extraction of holdings, transactions, and summaries using specs. |
| **Validator** | `src/validator/` | Multi-point financial reconciliation (cash balance, weight sum, ISIN, NAV). |
| **Learner & Redactor**| `src/learner/`, `src/redactor/` | PII redaction and LLM-assisted draft specification generation. |
| **LLM Adapter** | `src/llm_adapter/` | Unified OpenAI-compatible adapter supporting local Ollama (`llama3.2:3b`). |
| **Evaluation** | `src/eval/` | Ground-truth benchmarking and field-level accuracy comparison. |

---

## 3. Security Guarantees

1. **Zero Secret Logging**: Passwords, API tokens, and credentials are read strictly from `.env` and masked in `__repr__`.
2. **Read-Only IMAP**: Gmail IMAP operations explicitly set `readonly=True` to eliminate any risk of mailbox modification or deletion.
3. **PII Redaction**: When learning new formats via LLM, customer IDs, PAN numbers, and investor names are redacted before transmission.
4. **Isolated Storage**: Tests and evaluations use in-memory SQLite stores (`db_path=":memory:"`) to prevent database pollution.
