# Known Limitations & Scope

This document details current architectural boundaries, operational constraints, and technical assumptions of the PMS & AIF Statement Parser.

---

## 1. Input Document Types

- **Digital Vector PDFs Only**: The extraction pipeline relies on text coordinates and positional layout features extracted via `pdfplumber` / `pdfminer`. Purely scanned, photographed, or rasterized documents without embedded text layers require upstream OCR pre-processing before ingestion.
- **Password-Protected PDFs**: Encrypted PDFs are supported when valid candidate passwords are provided in memory or via the `--password` CLI flag. Brute-force cracking is intentionally not supported.

---

## 2. Template Drift & Learning

- **Quarantine over Mutation**: When a document matches a known layout within the near-match similarity window (`0.65 <= score < 0.90`), it is classified as `DRIFT_SUSPECTED` and quarantined. The system never mutates production specifications automatically.
- **Human-in-the-Loop Review**: Specifications learned by the LLM learner (`--learn`) are saved as draft proposals (`draft_*.json`). They remain inactive until human inspection and explicit promotion via `python -m src.cli --approve <DRAFT_ID>`.

---

## 3. Financial Statement Scope

- **Supported Formats**:
  - Format A (Standard & Messy PMS equity statements)
  - Format B (Alternative PMS statements)
  - Format C (AIF Category II / III capital statements with NAV valuation)
- **Institutional Variation**: Statements from providers with radically distinct tabular structures require creating or learning a dedicated `ParsingSpec`.

---

## 4. Live Integrations

- **Ollama**: Requires local installation of Ollama serving `llama3.2:3b` at `http://localhost:11434/v1`.
- **Gmail IMAP**: Ingestion is strictly read-only (`readonly=True`). Requires a Google App Password and 2-Step Verification.
