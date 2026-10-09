"""Intermediate Representation (IR) package for extracted PDF structures."""
from src.ir.exceptions import PDFExtractionError
from src.ir.extractor import extract_pdf
from src.ir.models import (
    BBox,
    DocumentIR,
    PageIR,
    TableCell,
    TableIR,
    TextBlock,
    WordIR,
)

__all__ = [
    "BBox",
    "WordIR",
    "TextBlock",
    "TableCell",
    "TableIR",
    "PageIR",
    "DocumentIR",
    "PDFExtractionError",
    "extract_pdf",
]
