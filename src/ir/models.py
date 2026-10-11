"""Intermediate Representation (IR) domain models for extracted PDF structures.

Preserves document metadata, page boundaries and dimensions, spatial text blocks,
individual words with bounding boxes, and tabular structures with cell data.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class BBox(BaseModel):
    """Bounding box coordinates (in points from top-left of page)."""

    model_config = ConfigDict(validate_assignment=True)

    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


class WordIR(BaseModel):
    """Single word token with bounding box and page reference."""

    model_config = ConfigDict(validate_assignment=True)

    text: str
    page_number: int
    bbox: BBox


class TextBlock(BaseModel):
    """Block/line of text preserving reading order and spatial position."""

    model_config = ConfigDict(validate_assignment=True)

    text: str
    page_number: int
    bbox: BBox | None = None
    words: list[WordIR] = Field(default_factory=list)


class TableCell(BaseModel):
    """Single cell within an extracted table."""

    model_config = ConfigDict(validate_assignment=True)

    text: str = ""
    row_index: int
    col_index: int
    bbox: BBox | None = None


class TableIR(BaseModel):
    """Structured table candidate extracted from a PDF page."""

    model_config = ConfigDict(validate_assignment=True)

    page_number: int
    bbox: BBox | None = None
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def col_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def get_headers(self) -> list[str]:
        """Return the first row of cells as headers, or an empty list if no rows exist."""
        return self.rows[0] if self.rows else []

    def get_data_rows(self) -> list[list[str]]:
        """Return all rows after the header row."""
        return self.rows[1:] if len(self.rows) > 1 else []


class PageIR(BaseModel):
    """Page-level representation preserving dimensions, text, blocks, and tables."""

    model_config = ConfigDict(validate_assignment=True)

    page_number: int  # 1-indexed
    width: float
    height: float
    raw_text: str = ""
    text_blocks: list[TextBlock] = Field(default_factory=list)
    words: list[WordIR] = Field(default_factory=list)
    tables: list[TableIR] = Field(default_factory=list)

    def get_text(self) -> str:
        """Return the best available text for this page.

        Preference order:
        1. ``raw_text`` if non-empty (always set by ``extract_pdf``).
        2. Words joined in reading order (available in test fixtures and
           digital PDFs that expose word tokens without a text layer).
        3. Table rows flattened to a space-joined string.
        """
        if self.raw_text:
            return self.raw_text
        # Fallback 1: words
        if self.words:
            return " ".join(w.text for w in self.words if w.text)
        # Fallback 2: table cell content
        if self.tables:
            parts: list[str] = []
            for t in self.tables:
                for row in t.rows:
                    parts.append(" ".join(cell for cell in row if cell))
            return " ".join(parts)
        return ""


class DocumentIR(BaseModel):
    """Top-level intermediate representation for an extracted PDF document."""

    model_config = ConfigDict(validate_assignment=True)

    source_path: str | None = None
    page_count: int = 0
    pages: list[PageIR] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def get_page(self, page_number: int) -> PageIR:
        """Retrieve a page by its 1-indexed page number."""
        for p in self.pages:
            if p.page_number == page_number:
                return p
        raise IndexError(f"Page {page_number} not found in document with {self.page_count} pages.")

    def get_all_text(self) -> str:
        """Concatenate the best available text from all pages in order.

        Delegates to ``PageIR.get_text()`` so that pages with populated
        ``words`` or ``tables`` but an empty ``raw_text`` (e.g. in-memory
        test fixtures and digital PDFs without a contiguous text layer)
        still produce meaningful content for classifiers and fingerprinters.
        """
        return "\n\n".join(p.get_text() for p in self.pages if p.get_text())

    def get_all_tables(self) -> list[TableIR]:
        """Flatten and return all tables across all pages."""
        return [t for p in self.pages for t in p.tables]
