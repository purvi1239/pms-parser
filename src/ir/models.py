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
        """Return the raw extracted text of the page."""
        return self.raw_text


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
        """Concatenate all raw text from all pages in order."""
        return "\n\n".join(p.raw_text for p in self.pages if p.raw_text)

    def get_all_tables(self) -> list[TableIR]:
        """Flatten and return all tables across all pages."""
        return [t for p in self.pages for t in p.tables]
