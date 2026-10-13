"""Field locator implementations for extracting scalar values from DocumentIR."""
from __future__ import annotations

import re
from typing import Any

from src.ir.models import BBox, DocumentIR, PageIR, WordIR
from src.specs.models import FieldLocator, LocatorType


def extract_text_in_bbox(page: PageIR, bbox: BBox) -> str:
    """Extract and concatenate text of all words intersecting the given bounding box."""
    matching_words: list[WordIR] = []
    margin = 2.0
    for w in page.words:
        if (
            w.bbox.x1 >= bbox.x0 - margin
            and w.bbox.x0 <= bbox.x1 + margin
            and w.bbox.bottom >= bbox.top - margin
            and w.bbox.top <= bbox.bottom + margin
        ):
            matching_words.append(w)

    if not matching_words:
        return ""

    # Sort in reading order (top rounded to baseline rows, then left-to-right)
    matching_words.sort(key=lambda w: (round(w.bbox.top / 4.0) * 4.0, w.bbox.x0))
    return " ".join(w.text for w in matching_words).strip()


def _strip_adjacent_label(val: str) -> str:
    """If val contains an adjacent field label (e.g. 'Statement Period: ...'), strip it.

    Safely handles single-space and multi-space adjacent labels without truncating
    legitimate multi-word values (e.g. 'PMS Quarterly Portfolio Report', 'John Doe',
    'Large Cap Equity / Growth') or values with punctuation/colons (e.g. times or ratios).
    """
    if not val or ":" not in val:
        return val

    # 1. 2+ spaces before any label ending in colon (standard column spacing)
    chunks = re.split(r"\s{2,}[A-Za-z0-9/\s]+:", val, maxsplit=1)
    if chunks and chunks[0].strip():
        val = chunks[0].strip()

    if ":" not in val:
        return val

    # 2. Single space before a recognized label pattern ending in colon.
    # Matches common multi-word statement label prefixes (e.g. Unrealised Gains:,
    # Realised Gains:, Statement Period:, Statement Type:, Report Period:,
    # Commitment Period:, Account Number:, Client ID:, etc.)
    # or a standalone single-word label (e.g. Date:, Currency:, Status:).
    chunks = re.split(
        r"\s+(?:(?:Unrealised|Realised|Statement|Report|Commitment|Account|Client|Investor|Fund|Base|Portfolio|Trade|Opening|Closing|Cash|Fees|Management|Total|Net|Units|NAV)\s+[A-Za-z0-9/]+|[A-Za-z/]+):",
        val,
        maxsplit=1,
    )
    if chunks and chunks[0].strip():
        return chunks[0].strip()

    return val


def _strip_parenthetical_prefix(val: str) -> str:
    """Strip leading parenthetical unit or description (e.g. '(INR)', '(Dividends/Int.)')
    from text before a numeric amount, while preserving negative numbers in parentheses
    (e.g. '(1,000.00)' or '(0.00)').
    """
    if not val or not val.startswith("("):
        return val

    # Must contain letters in the parenthetical to be a unit/description rather than a negative amount
    paren_match = re.match(r"^\(([^)]*[A-Za-z][^)]*)\)\s*:?\s*", val)
    if paren_match:
        remainder = val[paren_match.end():].strip().lstrip(":").strip()
        if remainder and re.search(r"[\d₹$€]", remainder):
            return remainder
    return val


def _extract_right_of_anchor_from_tables(page: PageIR, anchor: str) -> str | None:
    """Search tables for a cell matching the anchor and return text from the adjacent right cell.

    Performs a strict exact match first to prevent false matches against unrelated labels.
    If no exact match exists, falls back to parenthetical qualification matches (e.g.
    anchor 'Income' matching 'Income (Dividends/Int.)' or 'NAV Per Unit' matching
    'NAV Per Unit (INR)'). Unrelated labels like 'Income Tax' or 'Opening Portfolio Value'
    will not match 'Income' or 'Opening'.
    """
    anchor_clean = anchor.strip().rstrip(":").lower()
    if not anchor_clean:
        return None

    # Pass 1: Strict exact match (exact match or exact match + colon)
    for table in page.tables:
        for row in table.rows:
            for c_idx, cell in enumerate(row):
                cell_strip = cell.strip()
                cell_clean = cell_strip.rstrip(":").lower()

                # 1. Exact match with anchor in separate cell
                if cell_clean == anchor_clean or cell_clean.startswith(anchor_clean + ":"):
                    if c_idx + 1 < len(row):
                        candidate = row[c_idx + 1].strip()
                        if candidate != "":
                            cleaned = _strip_adjacent_label(candidate)
                            return cleaned if cleaned != "" else candidate

                # 2. Both anchor and value in same cell e.g. "Client ID: PWM-0042"
                if ":" in cell_strip:
                    parts = cell_strip.split(":", 1)
                    lbl = parts[0].strip().rstrip(":").lower()
                    if lbl == anchor_clean:
                        val = parts[1].strip()
                        if val != "":
                            cleaned = _strip_adjacent_label(val)
                            return cleaned if cleaned != "" else val

    # Pass 2: Bounded parenthetical suffix match (e.g. "Income (Dividends/Int.)" for "Income",
    # "NAV Per Unit (INR)" for "NAV Per Unit"). Does NOT match non-parenthetical words ("Total Fees" != "Total").
    paren_pattern = re.compile(r"^" + re.escape(anchor_clean) + r"(?:\s*\([^)]*\))?:?$")
    for table in page.tables:
        for row in table.rows:
            for c_idx, cell in enumerate(row):
                cell_strip = cell.strip()
                cell_clean = cell_strip.rstrip(":").lower()

                # 1. Parenthetical match in separate cell
                if paren_pattern.match(cell_clean):
                    if c_idx + 1 < len(row):
                        candidate = row[c_idx + 1].strip()
                        if candidate != "":
                            cleaned = _strip_adjacent_label(candidate)
                            return cleaned if cleaned != "" else candidate

                # 2. Both anchor and value in same cell
                if ":" in cell_strip:
                    parts = cell_strip.split(":", 1)
                    lbl = parts[0].strip().rstrip(":").lower()
                    if paren_pattern.match(lbl):
                        val = parts[1].strip()
                        if val != "":
                            cleaned = _strip_adjacent_label(val)
                            return cleaned if cleaned != "" else val

    return None


def _extract_right_of_anchor_from_text(page: PageIR, anchor: str, offset_x: float | None = None) -> str | None:
    """Search text blocks and lines for text appearing to the right of an anchor phrase."""
    anchor_clean = anchor.strip()
    if not anchor_clean:
        return None
    anchor_lower = anchor_clean.lower()

    # Check text blocks / lines first
    for block in page.text_blocks:
        text = block.text
        text_lower = text.lower()
        if anchor_lower in text_lower:
            idx = text_lower.find(anchor_lower)
            # Ensure the anchor is at word boundary / not part of a larger word
            if idx > 0 and text[idx - 1].isalnum():
                continue

            after = text[idx + len(anchor_clean):].strip()
            # If colon is next, strip it
            if after.startswith(":"):
                after = after[1:].strip()

            if after != "":
                after = _strip_parenthetical_prefix(after)
                cand = _strip_adjacent_label(after)
                if cand != "":
                    return cand

    # Check spatial word tokens if text block parsing did not yield a value
    words = page.words
    anchor_words = anchor_clean.split()
    if not anchor_words or not words:
        return None

    # Safe bounded search distance: default to 250.0 pt if not specified
    max_dist = offset_x if offset_x is not None else 250.0

    # Find sequence of words matching anchor words
    for i in range(len(words) - len(anchor_words) + 1):
        window = words[i : i + len(anchor_words)]
        if all(w.text.lower().rstrip(":") == aw.lower().rstrip(":") for w, aw in zip(window, anchor_words)):
            # Verify window is not preceded by an attached word on the same line (e.g. 'Fixed Income')
            if i > 0:
                prev_w = words[i - 1]
                if (
                    abs(prev_w.bbox.top - window[0].bbox.top) <= 5.0
                    and (window[0].bbox.x0 - prev_w.bbox.x1) < 12.0
                    and not prev_w.text.endswith(":")
                ):
                    continue

            last_anchor_word = window[-1]
            anchor_right = last_anchor_word.bbox.x1
            anchor_top = last_anchor_word.bbox.top

            # Gather words on the same line to the right
            right_words = [
                w
                for w in words
                if w.bbox.x0 >= anchor_right - 2.0
                and (w.bbox.x0 - anchor_right) <= max_dist
                and abs(w.bbox.top - anchor_top) <= 8.0
            ]
            if right_words:
                right_words.sort(key=lambda w: w.bbox.x0)
                extracted = " ".join(w.text for w in right_words).strip().lstrip(":")
                if extracted != "":
                    extracted = _strip_parenthetical_prefix(extracted)
                    cand = _strip_adjacent_label(extracted)
                    if cand != "":
                        return cand

    return None


def _extract_below_anchor(page: PageIR, anchor: str, offset_y: float | None = None) -> str | None:
    """Extract value positioned vertically beneath the specified anchor."""
    anchor_clean = anchor.strip().rstrip(":").lower()
    if not anchor_clean:
        return None

    # 1. Search tables (cell directly in next row, same column)
    # Pass 1: exact match
    for table in page.tables:
        for r_idx, row in enumerate(table.rows):
            for c_idx, cell in enumerate(row):
                if cell.strip().rstrip(":").lower() == anchor_clean:
                    if r_idx + 1 < len(table.rows) and c_idx < len(table.rows[r_idx + 1]):
                        val = table.rows[r_idx + 1][c_idx].strip()
                        if val != "":
                            return val

    # Pass 2: parenthetical match (e.g. "Units Held (Nos.)" for "Units Held")
    paren_pattern = re.compile(r"^" + re.escape(anchor_clean) + r"(?:\s*\([^)]*\))?:?$")
    for table in page.tables:
        for r_idx, row in enumerate(table.rows):
            for c_idx, cell in enumerate(row):
                if paren_pattern.match(cell.strip().rstrip(":").lower()):
                    if r_idx + 1 < len(table.rows) and c_idx < len(table.rows[r_idx + 1]):
                        val = table.rows[r_idx + 1][c_idx].strip()
                        if val != "":
                            return val

    # 2. Search spatial words beneath anchor
    words = page.words
    anchor_words = anchor_clean.split()
    if not words or not anchor_words:
        return None

    for i in range(len(words) - len(anchor_words) + 1):
        window = words[i : i + len(anchor_words)]
        if all(w.text.lower().rstrip(":") == aw.lower().rstrip(":") for w, aw in zip(window, anchor_words)):
            anchor_bbox_bottom = max(w.bbox.bottom for w in window)
            anchor_x0 = min(w.bbox.x0 for w in window)
            anchor_x1 = max(w.bbox.x1 for w in window)
            max_dist_y = offset_y if offset_y is not None else 60.0

            below_words = [
                w
                for w in words
                if w.bbox.top >= anchor_bbox_bottom - 2.0
                and (w.bbox.top - anchor_bbox_bottom) <= max_dist_y
                and w.bbox.x1 >= anchor_x0 - 20.0
                and w.bbox.x0 <= anchor_x1 + 40.0
            ]
            if below_words:
                below_words.sort(key=lambda w: (round(w.bbox.top / 4.0) * 4.0, w.bbox.x0))
                val = " ".join(w.text for w in below_words).strip()
                if val != "":
                    return val

    # 3. Check plain text lines (line immediately following anchor line)
    lines = page.raw_text.splitlines()
    for idx, line in enumerate(lines):
        if anchor_clean in line.lower() and idx + 1 < len(lines):
            next_line = lines[idx + 1].strip()
            if next_line != "":
                return next_line

    return None


def _extract_matching_pattern(page: PageIR, pattern: str) -> str | None:
    """Search page raw text and text blocks for a regex match."""
    regex = re.compile(pattern)
    # Search whole page raw_text
    match = regex.search(page.raw_text)
    if match:
        if match.groups():
            return match.group(1).strip()
        return match.group(0).strip()

    # Search block-by-block
    for block in page.text_blocks:
        b_match = regex.search(block.text)
        if b_match:
            if b_match.groups():
                return b_match.group(1).strip()
            return b_match.group(0).strip()

    return None


def extract_field_value(
    doc: DocumentIR,
    locator: FieldLocator,
    field_name: str = "",
) -> tuple[Any, list[str]]:
    """Extract a field value from DocumentIR according to FieldLocator rules.

    Returns:
        tuple of (extracted_value, warnings_list)
    """
    warnings: list[str] = []
    target_pages: list[PageIR] = []

    if locator.page_number is not None:
        try:
            target_pages = [doc.get_page(locator.page_number)]
        except IndexError:
            warnings.append(
                f"Page {locator.page_number} specified for field '{field_name}' does not exist in document."
            )
            target_pages = []
    else:
        target_pages = doc.pages

    extracted: str | None = None

    # Primary strategy execution
    if locator.locator_type == LocatorType.BOUNDING_BOX and locator.bbox is not None:
        page = target_pages[0] if target_pages else (doc.pages[0] if doc.pages else None)
        if page:
            extracted = extract_text_in_bbox(page, locator.bbox)
    elif locator.locator_type == LocatorType.VALUE_MATCHING_PATTERN and locator.pattern:
        for page in target_pages:
            extracted = _extract_matching_pattern(page, locator.pattern)
            if extracted:
                break
    elif locator.locator_type == LocatorType.VALUE_RIGHT_OF_ANCHOR and locator.anchor:
        for page in target_pages:
            # 1. Try tables (e.g. meta key/value tables)
            extracted = _extract_right_of_anchor_from_tables(page, locator.anchor)
            if extracted:
                break
            # 2. Try text lines and spatial words
            extracted = _extract_right_of_anchor_from_text(page, locator.anchor, locator.offset_x)
            if extracted:
                break
    elif locator.locator_type == LocatorType.VALUE_BELOW_ANCHOR and locator.anchor:
        for page in target_pages:
            extracted = _extract_below_anchor(page, locator.anchor, locator.offset_y)
            if extracted:
                break

    # Fallback to bounding box if anchor-based strategy found nothing
    if not extracted and locator.fallback_bbox is not None:
        fb_page_num = locator.page_number or 1
        try:
            fb_page = doc.get_page(fb_page_num)
            fb_text = extract_text_in_bbox(fb_page, locator.fallback_bbox)
            if fb_text:
                extracted = fb_text
                warnings.append(
                    f"Anchor for field '{field_name}' not found; extracted value using fallback bounding box."
                )
        except IndexError:
            pass

    # Use default value if nothing extracted
    if not extracted and locator.default is not None:
        return locator.default, warnings

    return extracted, warnings
