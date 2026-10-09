"""Similarity comparison and matching engine for document format fingerprints."""
from __future__ import annotations

from typing import Sequence, Mapping

from src.fingerprint.models import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
)


def _jaccard_similarity(s1: set[str], s2: set[str]) -> float:
    """Compute Jaccard similarity between two sets of strings."""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


def _table_header_similarity(
    tables1: list[list[str]], tables2: list[list[str]]
) -> tuple[float, list[str]]:
    """Compare table headers accounting for vocabulary overlap and column ordering."""
    notes: list[str] = []
    if not tables1 and not tables2:
        return 1.0, ["Both documents have no tabular structures"]
    if not tables1 or not tables2:
        return 0.0, ["One document has tables while the other has none"]

    pair_scores: list[float] = []
    min_len = min(len(tables1), len(tables2))
    max_len = max(len(tables1), len(tables2))

    for i in range(min_len):
        h1 = tables1[i]
        h2 = tables2[i]
        jaccard = _jaccard_similarity(set(h1), set(h2))

        # Positional match ratio (penalizes column reordering or renamed positions)
        matches = sum(1 for a, b in zip(h1, h2) if a == b)
        pos_ratio = matches / max(len(h1), len(h2), 1)

        # Average vocabulary overlap and strict column order
        pair_score = 0.5 * jaccard + 0.5 * pos_ratio
        pair_scores.append(pair_score)

        if pair_score < 0.95 and jaccard >= 0.70:
            notes.append(f"Table {i} has column reordering or renamed headers (pos_ratio={pos_ratio:.2f})")
        elif pair_score < 0.50:
            notes.append(f"Table {i} has structurally different headers")

    avg_score = sum(pair_scores) / max_len
    return avg_score, notes


def _layout_similarity(fp1: FormatFingerprint, fp2: FormatFingerprint) -> float:
    """Compare layout dimensions: table count and column count sequences."""
    cols1 = fp1.layout_stats.column_counts
    cols2 = fp2.layout_stats.column_counts

    if not cols1 and not cols2:
        return 1.0
    if not cols1 or not cols2:
        return 0.0

    # Column count sequence match
    col_matches = sum(1 for c1, c2 in zip(cols1, cols2) if c1 == c2)
    col_sim = col_matches / max(len(cols1), len(cols2), 1)

    # Table count match
    tc1 = fp1.layout_stats.table_count
    tc2 = fp2.layout_stats.table_count
    tc_sim = 1.0 - (abs(tc1 - tc2) / max(tc1, tc2, 1))

    return max(0.0, 0.7 * col_sim + 0.3 * tc_sim)


def compare_fingerprints(
    fp1: FormatFingerprint,
    fp2: FormatFingerprint,
    match_threshold: float = 0.90,
    near_match_threshold: float = 0.65,
) -> FingerprintMatchResult:
    """Compare two format fingerprints using weighted similarity scoring.

    Weights:
      - Table header similarity: 50%
      - Anchor phrase similarity: 35%
      - Layout statistics similarity: 15%

    Args:
        fp1: First format fingerprint (e.g. query document).
        fp2: Second format fingerprint (e.g. known template).
        match_threshold: Threshold above which document is considered KNOWN.
        near_match_threshold: Threshold above which document is DRIFT_SUSPECTED.

    Returns:
        FingerprintMatchResult with status, score, component breakdown, and reasons.
    """
    reasons: list[str] = []

    # 1. Edge case: completely empty/sparse fingerprints
    fp1_empty = not fp1.anchor_phrases and not fp1.table_headers
    fp2_empty = not fp2.anchor_phrases and not fp2.table_headers
    if fp1_empty and fp2_empty:
        return FingerprintMatchResult(
            status=FingerprintMatchStatus.KNOWN,
            similarity_score=1.0,
            matched_format_id=fp2.format_id,
            component_scores={"table_header": 1.0, "anchor": 1.0, "layout": 1.0},
            reasons=["Both fingerprints are empty"],
        )
    if fp1_empty or fp2_empty:
        return FingerprintMatchResult(
            status=FingerprintMatchStatus.UNKNOWN,
            similarity_score=0.0,
            matched_format_id=None,
            component_scores={"table_header": 0.0, "anchor": 0.0, "layout": 0.0},
            reasons=["One fingerprint is completely empty while the other is not"],
        )

    # 2. Table header similarity (50%)
    table_sim, table_notes = _table_header_similarity(fp1.table_headers, fp2.table_headers)
    reasons.extend(table_notes)

    # 3. Anchor phrase similarity (35%)
    anchors1 = set(fp1.anchor_phrases)
    anchors2 = set(fp2.anchor_phrases)
    anchor_sim = _jaccard_similarity(anchors1, anchors2)

    # 4. Layout statistics similarity (15%)
    layout_sim = _layout_similarity(fp1, fp2)

    # 5. Composite score
    total_score = round(0.50 * table_sim + 0.35 * anchor_sim + 0.15 * layout_sim, 4)

    component_scores = {
        "table_header": round(table_sim, 4),
        "anchor": round(anchor_sim, 4),
        "layout": round(layout_sim, 4),
    }

    # 6. Assign status
    if total_score >= match_threshold:
        status = FingerprintMatchStatus.KNOWN
        reasons.append(
            f"Similarity score ({total_score:.3f}) >= match threshold ({match_threshold}): known format"
        )
    elif total_score >= near_match_threshold:
        status = FingerprintMatchStatus.DRIFT_SUSPECTED
        reasons.append(
            f"Similarity score ({total_score:.3f}) within drift range [{near_match_threshold}, {match_threshold}): template drift suspected"
        )
    else:
        status = FingerprintMatchStatus.UNKNOWN
        reasons.append(
            f"Similarity score ({total_score:.3f}) < near-match threshold ({near_match_threshold}): unknown format"
        )

    return FingerprintMatchResult(
        status=status,
        similarity_score=total_score,
        matched_format_id=fp2.format_id,
        component_scores=component_scores,
        reasons=reasons,
    )


def match_fingerprint(
    query_fp: FormatFingerprint,
    known_fingerprints: Sequence[FormatFingerprint] | Mapping[str, FormatFingerprint],
    match_threshold: float = 0.90,
    near_match_threshold: float = 0.65,
) -> FingerprintMatchResult:
    """Find the best matching known fingerprint for a given query fingerprint.

    Args:
        query_fp: Fingerprint of the incoming document.
        known_fingerprints: Collection or dictionary of known format fingerprints.
        match_threshold: High threshold for KNOWN match.
        near_match_threshold: Lower threshold for DRIFT_SUSPECTED match.

    Returns:
        Best FingerprintMatchResult found, or UNKNOWN if collection is empty.
    """
    if isinstance(known_fingerprints, Mapping):
        candidates = list(known_fingerprints.values())
    else:
        candidates = list(known_fingerprints)

    if not candidates:
        return FingerprintMatchResult(
            status=FingerprintMatchStatus.UNKNOWN,
            similarity_score=0.0,
            matched_format_id=None,
            component_scores={},
            reasons=["No known fingerprints registered"],
        )

    best_result: FingerprintMatchResult | None = None

    for target in candidates:
        res = compare_fingerprints(
            query_fp,
            target,
            match_threshold=match_threshold,
            near_match_threshold=near_match_threshold,
        )
        if best_result is None or res.similarity_score > best_result.similarity_score:
            best_result = res

    assert best_result is not None
    # If the best result is UNKNOWN, clear matched_format_id
    if best_result.status == FingerprintMatchStatus.UNKNOWN:
        best_result.matched_format_id = None

    return best_result
