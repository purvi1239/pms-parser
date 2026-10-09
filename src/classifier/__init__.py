"""Document classifier module for detecting PMS and AIF investment statements."""
from src.classifier.classifier import classify_document
from src.classifier.models import ClassificationDecision, ClassificationResult

__all__ = [
    "classify_document",
    "ClassificationDecision",
    "ClassificationResult",
]
