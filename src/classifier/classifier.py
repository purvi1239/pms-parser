"""Deterministic rule-based document classifier for PMS and AIF statements."""
from __future__ import annotations

import re
from typing import Any

from src.classifier.models import ClassificationDecision, ClassificationResult
from src.classifier.rules import (
    AIF_STRONG_PHRASES,
    HOLDINGS_HEADER_KEYWORDS,
    IRRELEVANT_INDICATORS,
    PMS_STRONG_PHRASES,
    STATEMENT_ANCHORS,
    SUMMARY_HEADER_KEYWORDS,
    SUPPORTING_FINANCIAL_TERMS,
    TRANSACTION_HEADER_KEYWORDS,
)
from src.ir.models import DocumentIR
from src.models.enums import StatementType


def classify_document(doc: DocumentIR) -> ClassificationResult:
    """Classify a DocumentIR as STATEMENT (PMS / AIF), MAYBE, or SKIP.

    Args:
        doc: Intermediate Representation of the document.

    Returns:
        ClassificationResult with decision, statement_type, confidence, score, and reasons.
    """
    reasons: list[str] = []
    signals: dict[str, Any] = {
        "pms_matches": [],
        "aif_matches": [],
        "anchor_matches": [],
        "supporting_matches": [],
        "negative_matches": [],
        "table_signals": [],
    }

    # 1. Edge cases: empty document or no pages
    if doc.page_count == 0 or not doc.pages:
        return ClassificationResult(
            decision=ClassificationDecision.SKIP,
            statement_type=None,
            confidence=0.0,
            score=0,
            reasons=["Document contains zero pages"],
            signals=signals,
        )

    full_text = doc.get_all_text()
    if not full_text.strip():
        return ClassificationResult(
            decision=ClassificationDecision.SKIP,
            statement_type=None,
            confidence=0.0,
            score=0,
            reasons=["Document text is empty"],
            signals=signals,
        )

    text_lower = full_text.lower()

    # 2. Check for negative / irrelevant document indicators
    negative_score = 0
    for indicator in IRRELEVANT_INDICATORS:
        if indicator in text_lower:
            negative_score += 35
            signals["negative_matches"].append(indicator)
            reasons.append(f"Found non-statement indicator: '{indicator}'")

    # 3. Check for strong PMS phrases
    pms_score = 0
    for phrase in PMS_STRONG_PHRASES:
        if phrase in text_lower:
            pms_score += 35
            signals["pms_matches"].append(phrase)
            reasons.append(f"Found strong PMS phrase: '{phrase}'")

    # Also detect word boundary matches for "PMS" in title/context
    if re.search(r"\bpms\b", text_lower) and not any("pms" in m for m in signals["pms_matches"]):
        pms_score += 20
        signals["pms_matches"].append("pms")
        reasons.append("Found standalone term 'PMS'")

    # 4. Check for strong AIF phrases
    aif_score = 0
    for phrase in AIF_STRONG_PHRASES:
        if phrase in text_lower:
            aif_score += 35
            signals["aif_matches"].append(phrase)
            reasons.append(f"Found strong AIF phrase: '{phrase}'")

    if re.search(r"\baif\b", text_lower) and not any("aif" in m for m in signals["aif_matches"]):
        aif_score += 20
        signals["aif_matches"].append("aif")
        reasons.append("Found standalone term 'AIF'")

    # 5. Check common statement structural anchors
    anchor_score = 0
    for anchor in STATEMENT_ANCHORS:
        if anchor in text_lower:
            anchor_score += 15
            signals["anchor_matches"].append(anchor)
            reasons.append(f"Found statement anchor: '{anchor}'")
    # Cap anchor score to prevent repetition inflating total
    anchor_score = min(45, anchor_score)

    # 6. Check supporting financial vocabulary (generic words alone never suffice)
    supporting_count = 0
    for term in SUPPORTING_FINANCIAL_TERMS:
        # Match as whole word or phrase
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text_lower):
            supporting_count += 1
            signals["supporting_matches"].append(term)

    supporting_score = 0
    if supporting_count == 1:
        # A single generic financial word alone gives very low weight
        supporting_score = 5
        reasons.append(f"Found single generic financial term: '{signals['supporting_matches'][0]}'")
    elif supporting_count == 2:
        supporting_score = 10
        reasons.append(f"Found supporting financial terms: {signals['supporting_matches']}")
    elif supporting_count >= 3:
        # Capped so supporting vocabulary alone cannot classify as STATEMENT
        supporting_score = min(20, 10 + supporting_count * 2)
        reasons.append(f"Found multiple financial terms: {signals['supporting_matches'][:4]}")

    # 7. Check table structures from DocumentIR
    table_score = 0
    tables = doc.get_all_tables()
    if tables:
        for t in tables:
            headers = [h.lower().strip() for h in t.get_headers()]
            joined_headers = " ".join(headers)

            # Holdings table header matches
            holdings_matches = sum(1 for kw in HOLDINGS_HEADER_KEYWORDS if any(kw in h for h in headers))
            if holdings_matches >= 2:
                table_score += 25
                pms_score += 10
                signals["table_signals"].append("holdings_table")
                reasons.append("Found structured holdings table with security/cost/value columns")
                break

            # Transaction table header matches
            txn_matches = sum(1 for kw in TRANSACTION_HEADER_KEYWORDS if any(kw in h for h in headers))
            if txn_matches >= 2:
                table_score += 20
                signals["table_signals"].append("transaction_table")
                reasons.append("Found structured transaction table with date/activity/amount columns")
                break

            # Valuation summary header / labels
            if any(kw in joined_headers for kw in SUMMARY_HEADER_KEYWORDS):
                table_score += 15
                signals["table_signals"].append("summary_table")
                reasons.append("Found valuation summary table elements")
                break

    table_score = min(35, table_score)

    # 8. Compute total raw score
    raw_positive = pms_score + aif_score + anchor_score + supporting_score + table_score
    total_score = max(0, raw_positive - negative_score)

    # 9. Determine Decision and Confidence
    # Thresholds:
    #   >= 50: STATEMENT
    #   25 - 49: MAYBE
    #   < 25: SKIP
    statement_type: StatementType | None = None

    if total_score >= 50:
        decision = ClassificationDecision.STATEMENT
        # Confidence scaled between 0.80 and 0.99
        confidence = round(min(0.99, max(0.80, 0.75 + (total_score / 200.0) * 0.24)), 2)
        # Determine PMS vs AIF
        if aif_score > pms_score:
            statement_type = StatementType.AIF
            reasons.append("Classified as AIF based on fund/unit/commitment markers")
        elif pms_score > 0 or "holdings_table" in signals["table_signals"]:
            statement_type = StatementType.PMS
            reasons.append("Classified as PMS based on portfolio/holdings markers")
        else:
            # High score from generic statement anchors + tables, but specific type ambiguous
            statement_type = None

    elif total_score >= 25:
        decision = ClassificationDecision.MAYBE
        confidence = round(min(0.70, max(0.40, 0.40 + (total_score / 100.0) * 0.25)), 2)
        reasons.append("Ambiguous evidence; routed to manual review queue")
        if aif_score > pms_score and aif_score > 0:
            statement_type = StatementType.AIF
        elif pms_score > aif_score and pms_score > 0:
            statement_type = StatementType.PMS

    else:
        decision = ClassificationDecision.SKIP
        confidence = round(max(0.0, min(0.30, total_score / 100.0)), 2)
        reasons.append("Insufficient evidence to classify as financial statement")

    return ClassificationResult(
        decision=decision,
        statement_type=statement_type,
        confidence=confidence,
        score=total_score,
        reasons=reasons,
        signals=signals,
    )
