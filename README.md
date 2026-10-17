# PMS & AIF Investment Statement Parser

A deterministic, multi-format financial statement ingestion, parsing, and validation engine designed for **Portfolio Management Services (PMS)** and **Alternative Investment Funds (AIF)** statements.

---

## 1. Prerequisites

- **Python**: Python 3.10 or higher.
- **Environment**: Virtual environment with dependencies installed.

```bash
# Activate your virtual environment:
source .venv/bin/activate

# Or create a new environment:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Generate the synthetic statement test suite:
python -m src.sample_generator

# Seed the specs/ directory with approved parsing specifications for formats A, B, and C.
# This must be run once after generating samples (or any time specs/ is empty):
PYTHONPATH=. .venv/bin/python scripts/bootstrap_specs.py
```

---

## 2. How to Use

This section is a start-to-finish walkthrough for running the parser on your own statements. Detailed demos and integration setup follow in later sections.

### 2.1 Set Up

```bash
git clone https://github.com/purvi1239/pms-parser.git
cd pms-parser

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Generate synthetic samples, then seed the built-in specs (one-time):
python -m src.sample_generator
PYTHONPATH=. .venv/bin/python scripts/bootstrap_specs.py

# See every option your installed version supports:
python -m src.cli --help
```

Optional extras:
- **Ollama** (local LLM) is only needed if you want the parser to learn unfamiliar formats. See [Section 5](#5-stage-3-live-integrations-ollama--gmail-imap).
- **Gmail credentials** are only needed if you want to pull statements from a mailbox.

### 2.2 Parse a Single Statement

```bash
python -m src.cli path/to/statement.pdf
```

The parser fingerprints the layout, matches it to a known specification, extracts the data, and runs financial validation checks. The result is printed with a status (see [2.7](#27-reading-the-result)).

### 2.3 Parse a Folder of Statements

```bash
python -m src.cli --folder path/to/statements/
```

Every PDF in the folder goes through the same pipeline. Exact duplicates are detected and marked `DUPLICATE`.

### 2.4 Parse a Password-Protected PDF

```bash
python -m src.cli path/to/statement.pdf --password "<PASSWORD>"
```

Without a password, the document is marked `NEEDS_PASSWORD`. The password is used in memory only and is never logged or stored.

### 2.5 Pull Statements from Gmail (Optional, Read-Only)

1. Add `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and `GMAIL_MAILBOX` to `.env` (setup steps in [Section 5.2](#52-gmail-imap-integration-opt-in--read-only)).
2. Verify the connection, then ingest:

```bash
python -m src.cli --verify-gmail
python -m src.cli --gmail INBOX --limit 5
```

The mailbox is opened read-only. Nothing is marked, moved, deleted or sent.

### 2.6 Handle an Unknown or Changed Format

If a statement's layout is not recognised, it is not guessed at. Instead:

```bash
# 1. Let the LLM learner propose a draft spec (requires Ollama running):
python -m src.cli path/to/unknown-statement.pdf --learn

# 2. See the drafts waiting for review:
python -m src.cli --list-drafts

# 3. Open the draft in specs/draft_<name>.json, check it against the PDF,
#    then approve it:
python -m src.cli --approve <draft_id>
```

After approval, that format is parsed deterministically with no further LLM calls. Always review a draft before approving; a draft that was generated successfully is not necessarily a correct one.

If a known format has changed slightly (reordered columns, renamed headers), the document is quarantined as `DRIFT_SUSPECTED` and waits for human review rather than being parsed with a stale spec.

### 2.7 Reading the Result

| Status | Meaning | What to do |
|---|---|---|
| `PARSED` | Parsed and all applicable checks passed. | Nothing. |
| `PARSED_WITH_WARNINGS` | Parsed, but one or more checks flagged something. | Review the warnings against the PDF. |
| `NEEDS_REVIEW` | Format is unknown or could not be handled. | Run with `--learn`, or review manually. |
| `DRIFT_SUSPECTED` | Layout resembles a known format but has changed. | Review, then update or approve a spec. |
| `NEEDS_PASSWORD` | PDF is encrypted. | Re-run with `--password`. |
| `DUPLICATE` | Same document already processed. | Nothing. |
| `SKIPPED` | Not processed further. | Check the reason in the output. |
| `LEARNING_FAILED` | The learning workflow did not complete. | Check Ollama is running, then retry. |

Parsed data and validation findings are stored in SQLite at `data/pms_parser.db` and can be read through `StatementStore`. Validation checks include cash balance flow, portfolio weights summing to ~100%, `quantity * price ≈ market_value`, ISIN checksums, and (for AIFs) `units * NAV ≈ closing value`.

### 2.8 Try It Without Your Own Files

```bash
python demo.py            # Stage 1: parsing, validation, drift, encryption
python demo_stage2.py     # Stage 2: learning and spec reuse
```

Add `--verbose` to either for full diagnostics. These demos run on synthetic statements and do not prove that every real-world format is supported.


---

## 3. Stage 1 Synthetic Demo

Stage 1 validates the end-to-end ingestion, layout recognition, financial validation, drift quarantine, and password handling using the deterministic synthetic test suite located in `samples/synthetic/`.

### Running the Demo

To run the complete deterministic demo:

```bash
python demo.py
```

To display full per-document diagnostics (including classification signals, document IDs, file hashes, and detailed reconciliation data):

```bash
python demo.py --verbose
```

### Scenarios Covered by the Demo

1. **Scenario 1: Known-Format PMS Statement (`samples/synthetic/format_a1.pdf`)**
   - **Behavior**: Layout fingerprint is extracted and matched against the active `synthetic-format-a` specification.
   - **Extraction**: Extracts portfolio metadata (provider, strategy, client ID, statement period), portfolio summary (opening/closing valuation, cash balances, fees, income, gains), equity holdings (securities, ISINs, quantities, costs, market values, weights), and cash transactions.
   - **Reconciliation**: Compares parsed values against `samples/synthetic/format_a1.json` using the evaluation comparator to ensure 100% field accuracy.
   - **Validation**: Executes financial reconciliation checks including cash balance flow (`opening_cash + sum(txns) = closing_cash`), portfolio weights sum (`~100%`), holdings valuation (`quantity * price ≈ market_value`), and ISIN Luhn checksums. Inapplicable checks (e.g. AIF NAV consistency) are cleanly recorded as skipped.

2. **Scenario 2: Known-Format AIF Statement (`samples/synthetic/format_c.pdf`)**
   - **Behavior**: Recognizes Category II AIF statement layout (`synthetic-format-c`).
   - **Extraction**: Parses fund unit summary (units held, NAV per unit, closing portfolio value) and cash ledger transactions (inflows, outflows, management fees).
   - **Validation**: Verifies NAV consistency (`units held * NAV per unit ≈ closing fund value`), cash ledger reconciliation, and notes that individual equity weights are skipped because AIF investors hold fund units rather than individual securities.

3. **Scenario 3: Layout Drift Detection & Quarantine (`samples/synthetic/format_a_drift.pdf`)**
   - **Behavior**: Processes a mutated Format A document (reordered columns, renamed headers, altered section titles).
   - **Safeguard**: Fingerprint comparator produces a similarity score within the near-match band (`0.65 <= similarity < 0.90`), triggering `DRIFT_SUSPECTED`.
   - **Quarantine**: The document is quarantined for human review (`DocumentStatus.DRIFT_SUSPECTED`). The LLM learner is **not** called automatically, protecting production specifications from unvetted mutations.

4. **Scenario 4: Encrypted PDF Handling (`samples/synthetic/format_a1_encrypted.pdf`)**
   - **Part 4A (Without Password)**: Evaluates an encrypted statement without credentials. The parser safely traps the encrypted state, sets `DocumentStatus.NEEDS_PASSWORD`, and guides the user to provide candidate passwords via the CLI.
   - **Part 4B (With Password)**: Evaluates the same statement with a candidate password provided in memory. The document decrypts, matches `synthetic-format-a`, extracts all positions, and validates without ever logging, printing, or storing the password in plaintext.

---

## 4. Stage 2 Learning & Spec-Reuse Demo

Stage 2 demonstrates the unknown-format learning and human-in-the-loop approval lifecycle using synthetic Format B (`samples/synthetic/format_b.pdf`) and the built-in offline mock LLM transport (`FakeLLMAdapter`).

### Running the Stage 2 Demo

```bash
# Run all 4 Stage 2 scenarios:
python demo_stage2.py

# Verbose output with detailed attempt diagnostics:
python demo_stage2.py --verbose
```

### Scenarios Covered by the Stage 2 Demo

- **Scenario 5A (Unknown Format)**: Processes `format_b.pdf` with no existing format_b specification and `enable_learning=False`. The pipeline safely marks the document `NEEDS_REVIEW` without mutating any specifications.
- **Scenario 5B (Learner & Redaction)**: Extracts `DocumentIR`, runs the `Redactor` to strip potential PII, dispatches to the mock LLM transport, validates the candidate specification against the IR, and saves a draft specification (`specs/draft_synthetic-format-b.json`) with status `PROPOSED`.
- **Scenario 5C (Human Approval)**: Simulates a reviewer inspecting the draft and approving it. Promotes `draft_synthetic-format-b` to an active specification (`specs/synthetic-format-b.json`) with `is_active=True`.
- **Scenario 5D (Spec Reuse / Later Run)**: A fresh pipeline (new `StatementStore`, same `SpecStore`) reprocesses `format_b.pdf`. The approved specification is matched deterministically by layout fingerprint and the statement parses to `PARSED` with zero LLM calls.

---

## 5. Stage 3 Live Integrations: Ollama & Gmail IMAP

Stage 3 verifies live external integrations for the PMS Parser:
1. **Local Ollama Integration**: Real LLM extraction using the locally installed `llama3.2:3b` model over Ollama's local OpenAI-compatible API (`http://localhost:11434/v1`).
2. **Gmail IMAP Integration**: Attachment discovery and ingestion over SSL (`imap.gmail.com:993`) using Google App Passwords in strictly read-only mode.

### 5.1 Local Ollama Integration (`llama3.2:3b`)

The PMS Parser connects directly to local Ollama instances without external network calls or cloud API keys.

#### Ollama Setup
1. Install Ollama and pull the `llama3.2:3b` model:
   ```bash
   ollama pull llama3.2:3b
   ```
2. Verify that Ollama is serving locally:
   ```bash
   ollama list
   # Verify port 11434 is listening:
   curl http://localhost:11434/api/tags
   ```
3. Configure environment variables in `.env`:
   ```bash
   LLM_API_BASE=http://localhost:11434/v1
   LLM_MODEL=llama3.2:3b
   ```
   *(Note: `LLM_API_KEY` is not required for local Ollama endpoints).*

#### Verifying Ollama
Run the integration verification script:
```bash
# Verify local Ollama integration using synthetic prompt:
python verify_integrations.py --ollama

# Or via CLI:
python -m src.cli --verify-ollama
```

The verification makes a **real request** (never falls back to a mock) using synthetic financial holdings, verifies round-trip latency, confirms structured JSON extraction against schema, and records audit records in the LLM ledger.

---

### 5.2 Gmail IMAP Integration (Opt-In & Read-Only)

The parser includes an adapter for fetching financial statement PDF attachments directly from a Gmail mailbox.

#### Safety Guarantees
- **Strictly Read-Only**: Mailboxes are opened with `readonly=True` on IMAP `SELECT`. The parser **never** modifies, flags, marks as read, deletes, or sends emails.
- **Opt-In Only**: Gmail verification only runs when explicitly invoked with the `--gmail` flag. It is never triggered automatically by the demo or single-file parsing commands.
- **Credential Protection**: Passwords are read from `.env`, masked in `__repr__`, and never logged, printed, or exported in plaintext.

#### Gmail App Password Setup Instructions
1. **Enable 2-Step Verification**:
   Go to your [Google Account Security Settings](https://myaccount.google.com/signinoptions/two-step-verification) and turn on 2-Step Verification (required by Google for App Passwords).
2. **Generate an App Password**:
   Navigate to [Google App Passwords](https://myaccount.google.com/apppasswords).
   Create a new app password named `PMS Parser`. Google will display a 16-character passcode (e.g. `abcd efgh ijkl mnop`).
3. **Enable IMAP in Gmail**:
   In Gmail web interface -> Settings (gear icon) -> "See all settings" -> "Forwarding and POP/IMAP" -> Under "IMAP access", select **Enable IMAP** -> Click **Save Changes**.
4. **Configure `.env`**:
   Add the following variables to your `.env` file:
   ```bash
   GMAIL_ADDRESS=your.email@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   GMAIL_MAILBOX=INBOX
   ```

#### Verifying Gmail Connection (Read-Only)
Once configured, run the opt-in verification:
```bash
# Verify Gmail connection, SSL handshake, and mailbox selection:
python verify_integrations.py --gmail

# Or via CLI:
python -m src.cli --verify-gmail
```

To ingest statements from Gmail in production:
```bash
# Process statement attachments from INBOX:
python -m src.cli --gmail INBOX

# Process with custom search filter and message limit:
python -m src.cli --gmail INBOX --search "UNSEEN" --limit 10
```

---

## 6. CLI Usage

The command-line interface coordinates document processing, mailbox ingestion, specification approval, and integration checks:

```bash
# Process a single statement PDF:
python -m src.cli samples/synthetic/format_a1.pdf

# Process an encrypted statement PDF with candidate password:
python -m src.cli samples/synthetic/format_a1_encrypted.pdf --password "<PASSWORD>"

# Ingest an entire folder of statement PDFs in batch:
python -m src.cli --folder samples/synthetic/

# Ingest statements from Gmail mailbox:
python -m src.cli --gmail INBOX --limit 5

# Verify live local Ollama model (Stage 3):
python -m src.cli --verify-ollama

# Verify live Gmail IMAP connection (Stage 3, opt-in):
python -m src.cli --verify-gmail

# List draft specifications pending review:
python -m src.cli --list-drafts

# Approve and activate a draft specification:
python -m src.cli --approve draft_synthetic-format-b
```

---


## 7. Documentation

- [Architecture & Design Overview](docs/DESIGN.md): Subsystem responsibilities, pipeline state machines, and data flow.
- [AI Implementation & Engineering Log](docs/AI_LOG.md): Comprehensive chronological engineering log documenting all 17 build steps, design decisions, bug fixes, test counts, and system boundaries.
- [Known Limitations & Scope](docs/LIMITATIONS.md): Operational constraints, input document boundaries, and format scope.
