"""Format fingerprinting module for structural document matching and template drift detection."""
from src.fingerprint.comparator import compare_fingerprints, match_fingerprint
from src.fingerprint.extractor import extract_fingerprint
from src.fingerprint.models import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
    LayoutStats,
)

__all__ = [
    "extract_fingerprint",
    "compare_fingerprints",
    "match_fingerprint",
    "FormatFingerprint",
    "FingerprintMatchStatus",
    "FingerprintMatchResult",
    "LayoutStats",
]
