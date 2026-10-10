"""Tabular section parser implementing TABLE_AFTER_ANCHOR and multi-page flows."""
from __future__ import annotations

import re
from typing import Any

from src.engine.converters import clean_text, to_date, to_decimal, to_transaction_type
from src.ir.models import DocumentIR, PageIR, TableIR
from src.models.models import Holding, Transaction
from src.models.enums import TransactionType
from src.specs.models import LocatorType, TableLocator


def _normalize(s: str) -> str:
    """Normalize a header or anchor string for fuzzy matching."""
    return re.sub(r"[^\w\s]", "", s.strip().lower())


def _find_header_row_index(table: TableIR, keywords: list[str]) -> int:
    """Identify which row in the table contains column headers."""
    if not table.rows:
        return 0
    if not keywords:
        return 0

    norm_keywords = [_normalize(kw) for kw in keywords if kw.strip()]
    best_idx = 0
    best_score = -1

    # Check first 3 rows
    for r_idx in range(min(3, len(table.rows))):
        row = table.rows[r_idx]
        norm_cells = [_normalize(c) for c in row]
        score = 0
        for kw in norm_keywords:
            if any(kw in cell or cell in kw for cell in norm_cells if cell):
                score += 1
        if score > best_score:
            best_score = score
            best_idx = r_idx

    return best_idx if best_score > 0 else 0


def _resolve_column_indices(headers: list[str], column_mapping: dict[str, str]) -> dict[str, int]:
    """Map model fields to 0-based column indices based on header labels."""
    resolved: dict[str, int] = {}
    norm_headers = [_normalize(h) for h in headers]

    for model_field, col_spec in column_mapping.items():
        # 1. Direct numeric index e.g. "0", "1"
        if col_spec.strip().isdigit():
            resolved[model_field] = int(col_spec.strip())
            continue

        norm_spec = _normalize(col_spec)
        found_idx: int | None = None

        # 2. Exact match
        for idx, h in enumerate(norm_headers):
            if h == norm_spec:
                found_idx = idx
                break

        # 3. Substring match
        if found_idx is None:
            for idx, h in enumerate(norm_headers):
                if norm_spec in h or h in norm_spec:
                    found_idx = idx
                    break

        if found_idx is not None:
            resolved[model_field] = found_idx

    return resolved


def _is_header_repeat(row: list[str], header_keywords: list[str]) -> bool:
    """Check if a table row is a pagination repeat of the column headers."""
    if not header_keywords or not row:
        return False
    norm_keywords = [_normalize(kw) for kw in header_keywords if kw.strip()]
    norm_cells = [_normalize(c) for c in row if c.strip()]
    matches = sum(1 for kw in norm_keywords if any(kw in c for c in norm_cells))
    return matches >= min(2, len(norm_keywords))


def _find_candidate_tables(
    doc: DocumentIR,
    locator: TableLocator,
) -> tuple[list[tuple[int, TableIR]], list[str]]:
    """Locate the starting table and any continuation tables across pages."""
    warnings: list[str] = []
    start_page_num = locator.page_number or 1

    # 1. Find the page where the anchor appears
    anchor_clean = _normalize(locator.anchor) if locator.anchor else ""
    anchor_page_num: int | None = None

    if anchor_clean:
        for p in doc.pages:
            if p.page_number >= start_page_num:
                if anchor_clean in _normalize(p.raw_text):
                    anchor_page_num = p.page_number
                    break

    target_start_page = anchor_page_num or start_page_num
    matched_tables: list[tuple[int, TableIR]] = []

    # 2. On target page, locate the best matching TableIR
    try:
        page = doc.get_page(target_start_page)
    except IndexError:
        return [], [f"Table page {target_start_page} not found in document."]

    # If anchor position is known, candidate table should appear on or after the anchor
    best_table: TableIR | None = None
    if page.tables:
        if locator.header_keywords:
            # Score tables by header keyword matches
            scored = []
            for t in page.tables:
                score = 0
                for r in t.rows[:2]:
                    score += sum(1 for kw in locator.header_keywords if any(_normalize(kw) in _normalize(c) for c in r))
                scored.append((score, t))
            scored.sort(key=lambda s: s[0], reverse=True)
            if scored and scored[0][0] > 0:
                best_table = scored[0][1]

        if best_table is None and page.tables:
            # Default to first table if only one or no keywords match
            best_table = page.tables[0]

    if best_table is not None:
        matched_tables.append((target_start_page, best_table))

    # 3. Handle multi_page continuation tables
    if locator.multi_page and best_table is not None:
        expected_cols = best_table.col_count
        curr_page_num = target_start_page + 1

        while curr_page_num <= doc.page_count:
            try:
                next_page = doc.get_page(curr_page_num)
            except IndexError:
                break

            found_continuation = False
            for t in next_page.tables:
                # Table continues if column counts match or headers repeat
                cols_match = abs(t.col_count - expected_cols) <= 1
                headers_match = _is_header_repeat(t.rows[0] if t.rows else [], locator.header_keywords)

                if cols_match or headers_match:
                    matched_tables.append((curr_page_num, t))
                    found_continuation = True
                    break

            if not found_continuation:
                break
            curr_page_num += 1

    return matched_tables, warnings


def extract_raw_table_rows(
    doc: DocumentIR,
    locator: TableLocator,
) -> tuple[list[dict[str, str]], list[str]]:
    """Extract raw string row dictionaries from tables using TableLocator specifications."""
    tables_found, warnings = _find_candidate_tables(doc, locator)
    if not tables_found:
        warnings.append(f"No table matching anchor '{locator.anchor}' could be located.")
        return [], warnings

    first_page_num, first_table = tables_found[0]
    header_idx = _find_header_row_index(first_table, locator.header_keywords)
    headers = [c.strip() for c in first_table.rows[header_idx]]

    col_map = _resolve_column_indices(headers, locator.column_mapping)
    if not col_map:
        warnings.append(
            f"Failed to map any columns for table '{locator.anchor}' using headers {headers}."
        )
        return [], warnings

    stop_clean = _normalize(locator.stop_anchor) if locator.stop_anchor else ""
    data_rows: list[dict[str, str]] = []

    for table_page_num, table in tables_found:
        start_r = header_idx + 1 if table == first_table else 0

        for r_idx in range(start_r, len(table.rows)):
            row = table.rows[r_idx]

            # 1. Skip empty rows
            if not any(c.strip() for c in row):
                continue

            # 2. Check for stop anchor
            if stop_clean:
                row_norm = " ".join(_normalize(c) for c in row)
                if stop_clean in row_norm:
                    return data_rows, warnings

            # 3. Skip repeated header rows
            if _is_header_repeat(row, locator.header_keywords):
                continue

            # 4. Map columns to model fields
            row_dict: dict[str, str] = {}
            has_data = False
            for field, col_idx in col_map.items():
                if col_idx < len(row):
                    val = row[col_idx].strip()
                    row_dict[field] = val
                    if val:
                        has_data = True
                else:
                    row_dict[field] = ""

            if has_data:
                data_rows.append(row_dict)

    return data_rows, warnings


def parse_holdings_table(
    doc: DocumentIR,
    locator: TableLocator,
) -> tuple[list[Holding], list[str]]:
    """Parse holdings table into domain Holding instances."""
    raw_rows, warnings = extract_raw_table_rows(doc, locator)
    holdings: list[Holding] = []

    for idx, r in enumerate(raw_rows):
        sec_name = clean_text(r.get("security_name"))

        # Skip total / summary rows
        if sec_name and _normalize(sec_name) in ("total", "total holdings", "grand total", "subtotal"):
            continue

        isin = clean_text(r.get("isin"))

        # Decimal conversions with graceful warning recording
        qty = None
        if "quantity" in r:
            try:
                qty = to_decimal(r["quantity"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        cost = None
        if "cost" in r:
            try:
                cost = to_decimal(r["cost"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        mkt_val = None
        if "market_value" in r:
            try:
                mkt_val = to_decimal(r["market_value"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        weight = None
        if "weight" in r:
            try:
                weight = to_decimal(r["weight"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        holdings.append(
            Holding(
                security_name=sec_name,
                isin=isin,
                quantity=qty,
                cost=cost,
                market_value=mkt_val,
                weight=weight,
            )
        )

    return holdings, warnings


def parse_transactions_table(
    doc: DocumentIR,
    locator: TableLocator,
) -> tuple[list[Transaction], list[str]]:
    """Parse transactions table into domain Transaction instances."""
    raw_rows, warnings = extract_raw_table_rows(doc, locator)
    transactions: list[Transaction] = []

    for idx, r in enumerate(raw_rows):
        t_date = to_date(r.get("date"))

        # Transaction type mapping
        t_type = to_transaction_type(r.get("type"))
        if t_type is None:
            # Fallback if unparseable, record warning
            raw_type = r.get("type", "")
            warnings.append(f"Row {idx+1}: Unrecognized transaction type '{raw_type}'; defaulting to BUY.")
            t_type = TransactionType.BUY

        sec = clean_text(r.get("security"))

        qty = None
        if "quantity" in r:
            try:
                qty = to_decimal(r["quantity"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        amt = None
        if "amount" in r:
            try:
                amt = to_decimal(r["amount"])
            except ValueError as e:
                warnings.append(f"Row {idx+1}: {e}")

        transactions.append(
            Transaction(
                date=t_date,
                type=t_type,
                security=sec,
                quantity=qty,
                amount=amt,
            )
        )

    return transactions, warnings
