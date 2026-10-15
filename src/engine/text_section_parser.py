"""Text-section extractor for documents that lack usable PDF tables.

When a document presents holdings or transactions as plain text lines
(e.g. Alternative Investment Fund statements, some PMS formats) rather
than as embedded PDF table objects, this module provides a deterministic
extraction strategy driven entirely by spec-level config:

* ``TextSectionLocator`` — carried inside ``ParsingSpec.holdings_table``
  or ``ParsingSpec.transactions_table`` via the new ``TEXT_SECTION``
  locator type, OR supplied directly via ``spec.custom_tables``.
* ``extract_text_section_rows`` — public entry point that slices the
  document's raw text between ``anchor`` and ``stop_anchor``, then
  parses each line with ``row_pattern`` into a dict of named groups.

Design principles
-----------------
* Completely generic — no fund name, field value, or document structure
  is hard-coded here.
* Existing ``parse_holdings_table`` / ``parse_transactions_table`` callers
  are unchanged; the engine chooses this path only when the locator type
  is ``TEXT_SECTION``.
* All monetary / date conversion reuses the existing ``converters`` module
  so formatting is normalised consistently.
"""
from __future__ import annotations

import re
from typing import Any

from src.engine.converters import clean_text, to_date, to_decimal, to_transaction_type
from src.ir.models import DocumentIR
from src.models.enums import TransactionType
from src.models.models import Holding, Transaction


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.strip().lower())


def _slice_text_section(
    full_text: str,
    anchor: str | None,
    stop_anchor: str | None,
    start_page: int | None = None,
    page_texts: list[tuple[int, str]] | None = None,
) -> str:
    """Return the portion of *full_text* between anchor and stop_anchor.

    Matching is case-insensitive and ignores punctuation via *_normalize*.
    If *anchor* is None the full text is returned.
    If *stop_anchor* is None the section extends to end of document.

    Args:
        full_text: The combined raw text of all (or selected) pages.
        anchor: Section-start phrase (case-insensitive).
        stop_anchor: Section-end phrase (case-insensitive, exclusive).
        start_page: 1-indexed page to begin searching from (optional).
        page_texts: List of ``(page_number, page_raw_text)`` tuples used
            when *start_page* is given.

    Returns:
        The sliced text, or empty string if anchor not found.
    """
    # Optionally restrict to pages >= start_page
    if start_page is not None and page_texts:
        text_parts = [t for pn, t in page_texts if pn >= start_page]
        search_text = "\n".join(text_parts)
    else:
        search_text = full_text

    if not anchor:
        section = search_text
    else:
        anchor_lower = anchor.strip().lower()
        lines = search_text.splitlines()
        start_idx: int | None = None
        for i, line in enumerate(lines):
            if anchor_lower in line.lower():
                start_idx = i + 1  # data starts on the line after the anchor
                break
        if start_idx is None:
            return ""
        section = "\n".join(lines[start_idx:])

    if not stop_anchor:
        return section

    stop_lower = stop_anchor.strip().lower()
    result_lines: list[str] = []
    for line in section.splitlines():
        if stop_lower in line.lower():
            break
        result_lines.append(line)
    return "\n".join(result_lines)


def _parse_lines_with_pattern(
    text: str,
    row_pattern: str,
    skip_patterns: list[str] | None = None,
) -> list[dict[str, str]]:
    """Parse *text* line-by-line using *row_pattern* (a named-group regex).

    Lines that do not match *row_pattern* are silently skipped (they are
    considered headers, totals, blank separators, or continuation text).
    Lines that match any pattern in *skip_patterns* are also skipped even
    if they would otherwise match *row_pattern*.

    Args:
        text: Multi-line text slice to parse.
        row_pattern: A Python regex with **named groups** (``(?P<name>...)``).
            Each matched group name becomes a key in the returned dict.
        skip_patterns: Optional list of regex strings.  Any line fully
            matching one of these patterns is excluded from output.

    Returns:
        List of dicts, one per matched line, keyed by named group names.
    """
    compiled = re.compile(row_pattern)
    skips = [re.compile(sp) for sp in (skip_patterns or [])]
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(sk.search(line) for sk in skips):
            continue
        m = compiled.search(line)
        if m:
            rows.append({k: (v or "").strip() for k, v in m.groupdict().items()})
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_text_section_rows(
    doc: DocumentIR,
    anchor: str | None,
    stop_anchor: str | None,
    row_pattern: str,
    start_page: int | None = None,
    skip_patterns: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Extract structured rows from a plain-text section of the document.

    This is the deterministic, table-free extraction path.  It does not
    depend on PDF table detection; instead it uses the document's raw text
    lines parsed with a caller-supplied regex pattern.

    Args:
        doc: The ``DocumentIR`` to extract from.
        anchor: Phrase marking the start of the target section.
        stop_anchor: Phrase marking the end of the section (exclusive).
        row_pattern: Named-group regex applied to each line in the section.
        start_page: 1-indexed page number to start from (default: all pages).
        skip_patterns: Lines matching any of these are excluded.

    Returns:
        ``(rows, warnings)`` where *rows* is a list of dicts (one per
        matched line) and *warnings* is a list of diagnostic strings.
    """
    warnings: list[str] = []
    page_texts: list[tuple[int, str]] = [
        (p.page_number, p.raw_text) for p in doc.pages
    ]
    full_text = "\n".join(t for _, t in page_texts)

    section = _slice_text_section(
        full_text=full_text,
        anchor=anchor,
        stop_anchor=stop_anchor,
        start_page=start_page,
        page_texts=page_texts,
    )

    if not section.strip():
        label = f"anchor '{anchor}'" if anchor else "document start"
        warnings.append(
            f"Text section starting at {label} produced no content to parse."
        )
        return [], warnings

    try:
        rows = _parse_lines_with_pattern(section, row_pattern, skip_patterns)
    except re.error as exc:
        warnings.append(f"Invalid row_pattern regex: {exc}")
        return [], warnings

    if not rows:
        label = f"anchor '{anchor}'" if anchor else "document start"
        warnings.append(
            f"No lines in the text section starting at {label} matched row_pattern."
        )

    return rows, warnings


def parse_text_section_holdings(
    doc: DocumentIR,
    anchor: str | None,
    stop_anchor: str | None,
    row_pattern: str,
    column_mapping: dict[str, str],
    start_page: int | None = None,
    skip_patterns: list[str] | None = None,
) -> tuple[list[Holding], list[str]]:
    """Parse text-section rows into ``Holding`` instances.

    *column_mapping* maps model field names to the named groups produced by
    *row_pattern*, e.g.::

        {
            "security_name": "series",
            "quantity":       "units",
            "price":          "nav",
            "market_value":   "value",
        }

    Supported model fields: ``security_name``, ``isin``, ``quantity``,
    ``price``, ``cost``, ``market_value``, ``weight``.

    Args:
        doc: Source document.
        anchor: Section-start phrase.
        stop_anchor: Section-end phrase (exclusive).
        row_pattern: Named-group regex for data lines.
        column_mapping: Mapping from Holding field name → regex group name.
        start_page: Optional 1-indexed start page.
        skip_patterns: Lines to exclude even if they match *row_pattern*.

    Returns:
        ``(holdings, warnings)``
    """
    raw_rows, warnings = extract_text_section_rows(
        doc, anchor, stop_anchor, row_pattern, start_page, skip_patterns
    )
    holdings: list[Holding] = []
    _SKIP_NAMES = {"total", "total holdings", "grand total", "subtotal"}

    for idx, row in enumerate(raw_rows):
        # Remap via column_mapping (group_name → field_name)
        mapped: dict[str, str] = {}
        for field, group in column_mapping.items():
            mapped[field] = row.get(group, row.get(field, ""))

        sec_name = clean_text(mapped.get("security_name"))
        if sec_name and _normalize(sec_name) in _SKIP_NAMES:
            continue

        isin = clean_text(mapped.get("isin"))

        def _try_decimal(key: str) -> Any:
            raw = mapped.get(key, "").strip()
            if not raw:
                return None
            try:
                return to_decimal(raw)
            except ValueError as exc:
                warnings.append(f"Holdings row {idx + 1} field '{key}': {exc}")
                return None

        holdings.append(
            Holding(
                security_name=sec_name,
                isin=isin,
                quantity=_try_decimal("quantity"),
                price=_try_decimal("price"),
                cost=_try_decimal("cost"),
                market_value=_try_decimal("market_value"),
                weight=_try_decimal("weight"),
            )
        )

    return holdings, warnings


def parse_text_section_transactions(
    doc: DocumentIR,
    anchor: str | None,
    stop_anchor: str | None,
    row_pattern: str,
    column_mapping: dict[str, str],
    start_page: int | None = None,
    skip_patterns: list[str] | None = None,
) -> tuple[list[Transaction], list[str]]:
    """Parse text-section rows into ``Transaction`` instances.

    *column_mapping* maps model field names to named groups, e.g.::

        {
            "date":     "date",
            "type":     "particulars",
            "security": "series",
            "quantity": "units",
            "amount":   "contribution",
        }

    Supported model fields: ``date``, ``type``, ``security``, ``quantity``,
    ``price``, ``amount``.

    Args:
        doc: Source document.
        anchor: Section-start phrase.
        stop_anchor: Section-end phrase (exclusive).
        row_pattern: Named-group regex for data lines.
        column_mapping: Mapping from Transaction field name → regex group name.
        start_page: Optional 1-indexed start page.
        skip_patterns: Lines to exclude even if they match *row_pattern*.

    Returns:
        ``(transactions, warnings)``
    """
    raw_rows, warnings = extract_text_section_rows(
        doc, anchor, stop_anchor, row_pattern, start_page, skip_patterns
    )
    transactions: list[Transaction] = []

    for idx, row in enumerate(raw_rows):
        mapped: dict[str, str] = {}
        for field, group in column_mapping.items():
            mapped[field] = row.get(group, row.get(field, ""))

        t_date = to_date(mapped.get("date"))

        raw_type = mapped.get("type", "")
        t_type = to_transaction_type(raw_type)
        if t_type is None:
            warnings.append(
                f"Transaction row {idx + 1}: unrecognized type '{raw_type}'; defaulting to BUY."
            )
            t_type = TransactionType.BUY

        sec = clean_text(mapped.get("security"))

        def _try_decimal(key: str) -> Any:
            raw = mapped.get(key, "").strip()
            if not raw:
                return None
            try:
                return to_decimal(raw)
            except ValueError as exc:
                warnings.append(f"Transaction row {idx + 1} field '{key}': {exc}")
                return None

        transactions.append(
            Transaction(
                date=t_date,
                type=t_type,
                security=sec,
                quantity=_try_decimal("quantity"),
                price=_try_decimal("price"),
                amount=_try_decimal("amount"),
            )
        )

    return transactions, warnings
