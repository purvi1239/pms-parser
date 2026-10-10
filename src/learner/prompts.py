"""Prompt generators for the Learner loop and human review feedback adaptation.

Ensures all document text is redacted before sending to an LLM, and formats
clear system and user prompts for initial generation, repair retries, and
incorporating human review corrections.
"""
from __future__ import annotations

import json
from typing import Any

from src.ir.models import DocumentIR
from src.learner.models import HumanFeedback
from src.models.enums import StatementType
from src.redactor.redactor import Redactor

SYSTEM_PROMPT_SPEC_DESIGNER = """\
You are an expert financial document parsing engineer.
Your task is to analyze the layout and text of an investment portfolio statement and produce a deterministic ParsingSpec JSON object.

Locators must prefer anchor-relative strategies over fixed coordinates:
1. 'value_right_of_anchor': Finds value tokens to the right of an anchor phrase.
2. 'value_below_anchor': Finds value tokens below an anchor phrase.
3. 'value_matching_pattern': Finds a value matching a regex pattern near an anchor.
4. 'table_after_anchor': Extracts tables by specifying 'anchor', 'header_keywords', and 'column_mapping'.

Return ONLY a valid, parseable JSON object representing the ParsingSpec.
Do not wrap your response in unnecessary conversational text.
"""


def prepare_redacted_doc_context(
    doc: DocumentIR,
    redactor: Redactor,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract and redact document text and summarize table structures.

    Args:
        doc: The DocumentIR to process.
        redactor: The PII redactor to sanitize text.

    Returns:
        tuple[str, list[dict]]: (redacted_text, table_summaries).
    """
    raw_text = doc.get_all_text()
    redaction_result = redactor.redact(raw_text)
    redacted_text = redaction_result.redacted_text

    table_summaries: list[dict[str, Any]] = []
    for idx, table in enumerate(doc.get_all_tables()):
        headers = table.get_headers()
        sample_rows = table.get_data_rows()[:2]
        table_summaries.append(
            {
                "table_index": idx,
                "page_number": table.page_number,
                "headers": headers,
                "row_count": table.row_count,
                "sample_data": sample_rows,
            }
        )

    return redacted_text, table_summaries


def build_initial_prompt(
    doc: DocumentIR,
    format_id: str,
    statement_type: StatementType,
    redactor: Redactor,
) -> tuple[str, str]:
    """Construct initial generation prompt for an unknown document format."""
    redacted_text, tables = prepare_redacted_doc_context(doc, redactor)

    user_prompt = f"""\
Target Format ID: {format_id}
Statement Type: {statement_type.value}

Below is the sanitized, redacted text extracted from the source statement:
---
{redacted_text}
---

Extracted Table Candidates:
{json.dumps(tables, indent=2)}

Task:
Generate a deterministic ParsingSpec JSON object for this document format.
Include:
- 'format_id': "{format_id}"
- 'statement_type': "{statement_type.value}"
- 'metadata_fields': mapping of field names ('provider_name', 'strategy_name', 'client_identifier', 'statement_period', 'currency') to FieldLocator objects.
- 'summary_fields': mapping of summary fields ('opening_value', 'closing_value', 'opening_cash', 'closing_cash', etc.) to FieldLocator objects.
- 'holdings_table': TableLocator for portfolio holdings ('anchor', 'header_keywords', 'column_mapping').
- 'transactions_table': TableLocator for portfolio cash/trade transactions (if present).

Return the ParsingSpec JSON object:
"""
    return SYSTEM_PROMPT_SPEC_DESIGNER, user_prompt


def build_repair_prompt(
    doc: DocumentIR,
    format_id: str,
    previous_spec_json: str,
    errors: list[str],
    warnings: list[str],
    redactor: Redactor,
    repair_attempt: int = 1,
) -> tuple[str, str]:
    """Construct a repair prompt providing previous candidate spec and validation errors."""
    redacted_text, tables = prepare_redacted_doc_context(doc, redactor)

    errors_formatted = "\n".join(f"- {e}" for e in errors)
    warnings_formatted = "\n".join(f"- {w}" for w in warnings) if warnings else "None"

    user_prompt = f"""\
Specification Repair for format '{format_id}' (Attempt {repair_attempt})

The previously proposed ParsingSpec failed extraction or validation on the source document:

VALIDATION ERRORS:
{errors_formatted}

WARNINGS:
{warnings_formatted}

PREVIOUS SPECIFICATION:
{previous_spec_json}

SOURCE DOCUMENT TEXT (REDACTED):
---
{redacted_text}
---

Task:
Repair the ParsingSpec to fix each validation error listed above.
Check your locators, anchor phrases, regex patterns, or table column mappings so that
the engine extracts valid values that pass all financial reconciliation and identity checks.

Return the repaired ParsingSpec JSON object:
"""
    return SYSTEM_PROMPT_SPEC_DESIGNER, user_prompt


def build_feedback_prompt(
    doc: DocumentIR,
    format_id: str,
    feedback: HumanFeedback,
    base_spec_json: str | None,
    redactor: Redactor,
) -> tuple[str, str]:
    """Construct a prompt incorporating human review corrections."""
    redacted_text, tables = prepare_redacted_doc_context(doc, redactor)

    corrections_summary = {
        "metadata_corrections": feedback.corrected_metadata,
        "summary_corrections": feedback.corrected_summary,
        "holdings_sample": feedback.corrected_holdings[:3] if feedback.corrected_holdings else [],
        "transactions_sample": feedback.corrected_transactions[:3] if feedback.corrected_transactions else [],
        "reviewer_notes": feedback.notes,
    }

    base_spec_block = ""
    if base_spec_json:
        base_spec_block = f"\nEXISTING BASE SPECIFICATION:\n{base_spec_json}\n"

    user_prompt = f"""\
Target Format ID: {format_id}

A human reviewer verified the document and supplied the following required corrections:
{json.dumps(corrections_summary, indent=2, default=str)}
{base_spec_block}
SOURCE DOCUMENT TEXT (REDACTED):
---
{redacted_text}
---

TABLE STRUCTURES:
{json.dumps(tables, indent=2)}

Task:
Generate or update the ParsingSpec for format '{format_id}' so that the parsing engine
accurately extracts the exact values identified by the human reviewer, and satisfies all
validation checks (cash reconciliation, portfolio weights, ISIN checksums, etc.).

Return the updated ParsingSpec JSON object:
"""
    return SYSTEM_PROMPT_SPEC_DESIGNER, user_prompt
