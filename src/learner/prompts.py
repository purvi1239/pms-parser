"""Prompt generators for the Learner loop and human review feedback adaptation.

Ensures all document text is redacted before sending to an LLM, and formats
clear system and user prompts for initial generation, repair retries, and
incorporating human review corrections.
"""
from __future__ import annotations

import json
import re
from typing import Any

from src.ir.models import DocumentIR
from src.learner.models import HumanFeedback
from src.models.enums import StatementType
from src.redactor.redactor import Redactor

SYSTEM_PROMPT_SPEC_DESIGNER = """\
You are an expert financial document parsing engineer.
Your task is to analyze the layout and text of an investment portfolio statement and produce a deterministic ParsingSpec JSON object.

Locators must prefer anchor-relative strategies over fixed coordinates:
1. 'value_right_of_anchor': Finds value tokens to the right of an anchor phrase on the same line.
2. 'value_below_anchor': Finds value tokens below an anchor phrase (next line or spatial).
3. 'value_matching_pattern': Finds a value matching a named-group regex (e.g. "(?P<value>...)").
4. 'table_after_anchor': Extracts PDF-table objects using 'anchor', 'header_keywords', 'column_mapping'.
5. 'text_section': Extracts holdings or transactions from PLAIN TEXT when no usable PDF table exists.
   Fields required: "anchor" (section start), "stop_anchor" (section end), "row_pattern" (named-group regex),
   "column_mapping" (model field → regex group name).
   Supported model field names for text_holdings:
     security_name, isin, quantity, price, cost, market_value, weight
   Supported model field names for text_transactions:
     date, type, security, quantity, price, amount

CRITICAL RULES — you MUST follow every rule or the spec is rejected:
- NEVER return an empty spec (empty metadata_fields, empty summary_fields, null holdings, null transactions)
  when the document contains clearly extractable data.
- If the document has tables with holdings or transactions, use 'table_after_anchor'.
- If the document has NO usable PDF tables for holdings/transactions but the data appears as
  structured text lines, populate 'text_holdings' and/or 'text_transactions' with a 'text_section' locator.
- Every anchor phrase must appear verbatim (case-insensitive) in the document text shown to you.
- Every regex 'row_pattern' must use named groups: (?P<name>...).
- 'column_mapping' must map each Holding/Transaction model field to the correct group name.
- Do not invent field values or anchor phrases not present in the source document.
- Return ONLY a valid, parseable JSON object representing the ParsingSpec.
- Do not wrap your response in markdown code fences or conversational text.
"""

_STRUCTURAL_KEYWORDS = frozenset(
    {
        "portfolio",
        "holding",
        "holdings",
        "position",
        "positions",
        "transaction",
        "transactions",
        "activity",
        "trades",
        "ledger",
        "summary",
        "valuation",
        "performance",
        "allocation",
        "overview",
        "statement",
        "account",
        "client",
        "investor",
        "period",
        "currency",
        "opening",
        "closing",
        "nav",
        "corpus",
        "cash",
        "capital",
        "charges",
        "fee",
        "fees",
        "tax",
        "gain",
        "loss",
        "realized",
        "unrealized",
        "total",
        "subtotal",
        "sub total",
        "grand total",
        "net",
        "balance",
        "security",
        "scrip",
        "isin",
        "quantity",
        "qty",
        "price",
        "rate",
        "market value",
        "cost",
        "weight",
        "units",
        "amount",
        "particulars",
        "purchase",
        "sale",
        "redemption",
        "dividend",
        "interest",
    }
)


_GENERIC_PAGE_HEADER_RE = re.compile(
    r"^[-—\s]*(?:page\s+\d+(?:\s*(?:of|/)\s*\d+)?(?:\s*(?:header|title|footer|contd|\(contd\.?\)))?)[-—\s]*$",
    re.IGNORECASE,
)

_SPECIFIC_SECTION_KEYWORDS = frozenset(
    {
        "portfolio",
        "holding",
        "holdings",
        "position",
        "positions",
        "transaction",
        "transactions",
        "activity",
        "trades",
        "ledger",
        "summary",
        "valuation",
        "performance",
        "allocation",
        "overview",
        "nav",
        "corpus",
        "total",
        "subtotal",
        "sub total",
        "grand total",
        "capital",
        "client",
        "investor",
        "statement",
    }
)

_COLUMN_HEADER_KEYWORDS = frozenset(
    {
        "security",
        "scrip",
        "isin",
        "quantity",
        "qty",
        "price",
        "rate",
        "market value",
        "amount",
        "date",
        "weight",
        "particulars",
        "units",
        "cost",
    }
)


_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{7,10}\d\b")
_DATE_RE = re.compile(r"\b\d{1,2}[/.-](?:\d{1,2}|[A-Za-z]{3})[/.-]\d{2,4}\b")
_NUMERIC_TOKEN_RE = re.compile(r"\b\d+(?:[.,]\d+)*%?\b")


def _is_data_row(line: str) -> bool:
    """Return True if a line represents an individual holding or transaction data row."""
    stripped = line.strip()
    if not stripped:
        return False

    # Labeled key-value lines with a colon are NOT data rows
    if ":" in stripped:
        left, _, _ = stripped.partition(":")
        left_words = left.strip().split()
        if 0 < len(left_words) <= 6:
            return False

    lower = stripped.lower()

    # Section totals and generic page headers are NOT data rows
    if lower.startswith(("total", "subtotal", "sub total", "grand total", "net ")):
        return False
    if _GENERIC_PAGE_HEADER_RE.match(stripped):
        return False

    has_isin = bool(_ISIN_RE.search(stripped))
    has_date = bool(_DATE_RE.search(stripped))

    # Strip dates out before counting numeric tokens so date components (day/month/year)
    # are not mistaken for financial figures (qty, price, amount)
    text_without_dates = _DATE_RE.sub(" ", stripped)
    numeric_tokens = _NUMERIC_TOKEN_RE.findall(text_without_dates)

    # Section headings and summary titles should not be treated as data rows unless they contain an ISIN
    words = set(re.findall(r"[a-z]+", lower))
    if not has_isin and words.intersection(
        {"portfolio", "summary", "valuation", "performance", "allocation", "overview"}
    ):
        return False

    # Holdings row: has ISIN code and numeric figures
    if has_isin and len(numeric_tokens) >= 1:
        return True

    # Transaction row: has date and at least one numeric figure (qty, price, amount)
    if has_date and len(numeric_tokens) >= 1:
        return True

    # Multiple standalone numeric figures (e.g. qty, price, value, weight)
    if len(numeric_tokens) >= 2:
        return True

    return False


def _line_priority(line: str) -> int:
    """Return priority tier (1 highest to 4 lowest) for line retention under budget pressure."""
    stripped = line.strip()
    if not stripped:
        return 4
    lower = stripped.lower()

    # 4 (Lowest): Generic page headers
    if _GENERIC_PAGE_HEADER_RE.match(stripped):
        return 4

    # Data rows are prioritized below structural headings
    if _is_data_row(stripped):
        if _ISIN_RE.search(stripped) or _DATE_RE.search(stripped):
            return 2
        return 3

    # 1 (Highest): Specific section headings, labeled fields with ':', totals
    if ":" in stripped:
        left, _, _ = stripped.partition(":")
        left_words = left.strip().split()
        if 0 < len(left_words) <= 6:
            return 1

    if lower.startswith(("total", "subtotal", "sub total", "grand total", "net ")):
        return 1

    words = set(re.findall(r"[a-z0-9]+", lower))
    if any(k in words for k in _SPECIFIC_SECTION_KEYWORDS):
        return 1

    # 2 (High): Column headers
    column_matches = words.intersection(_COLUMN_HEADER_KEYWORDS)
    if len(column_matches) >= 2 or any(k in words for k in ("isin", "quantity", "price", "amount")):
        return 2

    return 3


def _is_structural_line(line: str) -> bool:
    """Return True if a line looks like a heading, label, table header, or total."""
    stripped = line.strip()
    if not stripped:
        return False

    # Individual security and transaction data rows are NOT structural lines
    if _is_data_row(stripped):
        return False

    # Generic page headers are not section/table structural lines
    if _GENERIC_PAGE_HEADER_RE.match(stripped):
        return False

    lower = stripped.lower()

    # Short line with a colon usually indicates a labeled field/metadata/summary/total
    if ":" in stripped:
        left, _, _ = stripped.partition(":")
        left_words = left.strip().split()
        if 0 < len(left_words) <= 6:
            return True

    # Starts with a total keyword
    if lower.startswith(("total", "subtotal", "sub total", "grand total", "net ")):
        return True

    words = set(re.findall(r"[a-z0-9]+", lower))

    # Section heading keyword check (e.g. portfolio, holdings, transactions, summary)
    if any(k in words for k in _SPECIFIC_SECTION_KEYWORDS):
        return True

    # Column header line check (contains at least 2 column header terms)
    column_matches = words.intersection(_COLUMN_HEADER_KEYWORDS)
    if len(column_matches) >= 2:
        return True

    # Short title or uppercase section marker without heavy numeric content
    digit_count = sum(c.isdigit() for c in stripped)
    if len(stripped) < 50 and digit_count <= 2:
        return True

    return False


def _condense_page_lines(lines: list[str], max_sample_rows: int = 4) -> list[str]:
    """Condense lines of a single page, preserving structural lines and sampling data rows."""
    if len(lines) <= 25:
        return lines

    result: list[str] = []
    data_buffer: list[str] = []

    def flush_data_buffer() -> None:
        nonlocal data_buffer
        if not data_buffer:
            return
        if len(data_buffer) <= max_sample_rows + 2:
            result.extend(data_buffer)
        else:
            # Keep first representative rows
            result.extend(data_buffer[:max_sample_rows])
            omitted = len(data_buffer) - max_sample_rows - 1
            result.append(f"... [{omitted} rows omitted] ...")
            # Keep the last representative row before next heading or end
            result.append(data_buffer[-1])
        data_buffer = []

    for line in lines:
        if _is_data_row(line):
            data_buffer.append(line)
        else:
            flush_data_buffer()
            result.append(line)

    flush_data_buffer()
    return result


def _extract_page_snippet(lines: list[str], budget: int) -> str:
    """Extract a representative snippet from a page's lines within a character budget."""
    if budget <= 0 or not lines:
        return ""

    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined

    priorities = [_line_priority(l) for l in lines]
    candidate_indices = sorted(range(len(lines)), key=lambda i: (priorities[i], i))

    chosen_indices: list[int] = []
    curr_len = 0

    for idx in candidate_indices:
        line = lines[idx]
        needed = len(line) + (1 if chosen_indices else 0)
        if curr_len + needed <= budget:
            chosen_indices.append(idx)
            curr_len += needed

    if not chosen_indices and budget > 0:
        top_idx = candidate_indices[0]
        return lines[top_idx][:budget]

    selected = [lines[i] for i in sorted(chosen_indices)]
    result = "\n".join(selected)
    if len(result) > budget:
        result = result[:budget]
    return result


def _select_representative_page_indices(total_pages: int, max_pages: int) -> list[int]:
    """Select 0-based page indices balancing early and late pages."""
    if max_pages >= total_pages:
        return list(range(total_pages))
    if max_pages <= 1:
        return [0]

    front_count = (max_pages + 1) // 2
    back_count = max_pages - front_count

    front_indices = list(range(front_count))
    back_indices = list(range(total_pages - back_count, total_pages))

    seen: set[int] = set()
    result: list[int] = []
    for idx in front_indices + back_indices:
        if idx not in seen and 0 <= idx < total_pages:
            seen.add(idx)
            result.append(idx)
    return sorted(result)


def build_bounded_doc_text(
    doc: DocumentIR,
    max_total_chars: int = 8000,
    max_sample_rows: int = 4,
) -> str:
    """Build a bounded representation of document text across all pages.

    The returned text is guaranteed to never exceed ``max_total_chars``, including
    page separators, regardless of page count or number of structural lines.
    Allocates available budget across pages, preserving useful headings,
    summary fields, and representative rows from early and late pages where possible.

    Args:
        doc: The DocumentIR to process.
        max_total_chars: Maximum characters allowed for bounded text.
        max_sample_rows: Number of representative rows to keep per section run.

    Returns:
        Bounded string containing representative text from across the document.
    """
    if max_total_chars <= 0:
        return ""

    if not doc.pages:
        raw_all = doc.get_all_text()
        return raw_all[:max_total_chars]

    # Step 1: Condense each page
    page_data: list[list[str]] = []
    for page in doc.pages:
        raw = page.get_text()
        if not raw.strip():
            continue
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        condensed_lines = _condense_page_lines(lines, max_sample_rows=max_sample_rows)
        if condensed_lines:
            page_data.append(condensed_lines)

    if not page_data:
        return ""

    # Check if all condensed pages already fit within max_total_chars
    all_page_texts = ["\n".join(lines) for lines in page_data]
    combined = "\n\n".join(all_page_texts)
    if len(combined) <= max_total_chars:
        return combined

    total_pages = len(page_data)

    # Determine how many pages we can meaningfully represent within the budget.
    # Aim for at least ~45-50 chars per page so identifying headings can fit.
    target_pages = max(1, min(total_pages, max_total_chars // 50))

    # Ensure target_pages leaves at least 1 content char after separators
    while target_pages > 1 and (target_pages - 1) * 2 >= max_total_chars:
        target_pages -= 1

    selected_indices = _select_representative_page_indices(total_pages, target_pages)
    num_selected = len(selected_indices)

    sep_cost = (num_selected - 1) * 2
    content_budget = max(0, max_total_chars - sep_cost)

    # Dynamically allocate content budget across selected pages
    remaining_budget = content_budget
    page_snippets: list[str] = []

    for i, idx in enumerate(selected_indices):
        pages_left = num_selected - i
        this_budget = remaining_budget // pages_left
        snippet = _extract_page_snippet(page_data[idx], this_budget)
        if snippet:
            page_snippets.append(snippet)
            remaining_budget -= len(snippet)

    if not page_snippets:
        first_line = page_data[0][0] if (page_data and page_data[0]) else ""
        return first_line[:max_total_chars]

    result = "\n\n".join(page_snippets)

    # Absolute invariant guarantee
    if len(result) > max_total_chars:
        result = result[:max_total_chars]

    return result


def build_bounded_doc_context(
    doc: DocumentIR,
    redactor: Redactor,
    max_total_chars: int = 8000,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract and redact document text within bounded limits and summarize table structures.

    Preserves text across all pages, including section headings, metadata labels,
    summary fields, and representative holdings/transaction rows.

    Args:
        doc: The DocumentIR to process.
        redactor: The PII redactor to sanitize text.
        max_total_chars: Maximum characters allowed for the text context.

    Returns:
        tuple[str, list[dict]]: (redacted_text, table_summaries).
    """
    bounded_text = build_bounded_doc_text(doc, max_total_chars=max_total_chars)
    redaction_result = redactor.redact(bounded_text)
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


def prepare_redacted_doc_context(
    doc: DocumentIR,
    redactor: Redactor,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract and redact document text and summarize table structures.

    Delegates to ``build_bounded_doc_context`` to ensure bounded text size
    across all pages while preserving section headings and representative rows.

    Args:
        doc: The DocumentIR to process.
        redactor: The PII redactor to sanitize text.

    Returns:
        tuple[str, list[dict]]: (redacted_text, table_summaries).
    """
    return build_bounded_doc_context(doc, redactor)


def _has_usable_tables(doc: DocumentIR) -> bool:
    """Return True when the document contains at least one multi-row PDF table."""
    return any(t.row_count > 1 for t in doc.get_all_tables())


def _table_hint(doc: DocumentIR) -> str:
    """Return a context hint advising whether to use table vs text-section locators."""
    if _has_usable_tables(doc):
        return (
            "PDF TABLE OBJECTS DETECTED: prefer 'table_after_anchor' for holdings/transactions."
        )
    return (
        "NO USABLE PDF TABLES DETECTED: use 'text_section' locator type in 'text_holdings' "
        "and/or 'text_transactions' to parse holdings and transactions from plain text lines."
    )


def build_initial_prompt(
    doc: DocumentIR,
    format_id: str,
    statement_type: StatementType,
    redactor: Redactor,
) -> tuple[str, str]:
    """Construct initial generation prompt for an unknown document format."""
    redacted_text, tables = build_bounded_doc_context(doc, redactor)
    table_hint = _table_hint(doc)

    user_prompt = f"""\
Target Format ID: {format_id}
Statement Type: {statement_type.value}

Document Layout Hint: {table_hint}

Below is the sanitized, redacted text extracted from the source statement.
Use this text verbatim to choose anchor phrases and verify that your row_pattern regex matches real lines:
---
{redacted_text}
---

Extracted PDF Table Candidates (empty if none):
{json.dumps(tables, indent=2)}

Task:
Generate a deterministic ParsingSpec JSON object for this document format.

Required top-level fields:
- 'format_id': "{format_id}"
- 'statement_type': "{statement_type.value}"
- 'metadata_fields': map at least the fields present in the document from:
    provider_name, strategy_name, client_identifier, statement_period, currency
  Each value is a FieldLocator object (locator_type, anchor, pattern, etc.).
- 'summary_fields': map at least the summary fields visible in the document from:
    opening_value, closing_value, opening_cash, closing_cash, fees_charged,
    income, units, nav_per_unit
  Each value is a FieldLocator object.
- If the document has portfolio holdings data:
  - With PDF tables: use 'holdings_table' (TableLocator, locator_type='table_after_anchor').
  - Without PDF tables: use 'text_holdings' (TextSectionLocator, locator_type='text_section').
- If the document has transaction/ledger data:
  - With PDF tables: use 'transactions_table' (TableLocator, locator_type='table_after_anchor').
  - Without PDF tables: use 'text_transactions' (TextSectionLocator, locator_type='text_section').

TextSectionLocator format:
{{
  "locator_type": "text_section",
  "anchor": "<section heading phrase from the document>",
  "stop_anchor": "<next section heading or end marker>",
  "row_pattern": "<Python regex with named groups, e.g. (?P<date>\\\\d{{2}}/\\\\d{{2}}/\\\\d{{4}})\\\\s+(?P<particulars>.+?)\\\\s+(?P<units>[\\\\d,\\\\.]+)>",
  "column_mapping": {{
    "<model_field>": "<named_group_in_row_pattern>",
    ...
  }},
  "skip_patterns": ["<regex matching total/header lines to exclude>"]
}}

Return only the ParsingSpec JSON object:
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
    redacted_text, tables = build_bounded_doc_context(doc, redactor)
    table_hint = _table_hint(doc)

    errors_formatted = "\n".join(f"- {e}" for e in errors)
    warnings_formatted = "\n".join(f"- {w}" for w in warnings) if warnings else "None"

    user_prompt = f"""\
Specification Repair for format '{format_id}' (Attempt {repair_attempt})

Document Layout Hint: {table_hint}

The previously proposed ParsingSpec failed extraction or validation on the source document:

VALIDATION ERRORS (you must fix all of these):
{errors_formatted}

WARNINGS (address if related to extraction failures):
{warnings_formatted}

PREVIOUS SPECIFICATION:
{previous_spec_json}

SOURCE DOCUMENT TEXT (REDACTED) — use this to verify anchor phrases exist and row_pattern matches real lines:
---
{redacted_text}
---

Extracted PDF Table Candidates (empty if none):
{json.dumps(tables, indent=2)}

Repair Instructions:
1. Fix every error listed above.
2. If an anchor phrase is not found in the document text, replace it with one that IS present.
3. If a 'table_after_anchor' locator extracted zero rows and no PDF table exists, switch to
   a 'text_section' locator in 'text_holdings' or 'text_transactions'.
4. If a 'text_section' row_pattern matched zero lines, rewrite it so it matches the actual
   line format shown in the document text.
5. Ensure 'column_mapping' maps every needed model field to a named group in 'row_pattern'.
6. Never return metadata_fields or summary_fields as empty when the document has extractable data.

Return only the repaired ParsingSpec JSON object:
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
    redacted_text, tables = build_bounded_doc_context(doc, redactor)

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
